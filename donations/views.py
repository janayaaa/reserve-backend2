import json
import random
import re
from decimal import Decimal
from django.db import transaction
from django.utils import timezone
from django.contrib.auth.hashers import make_password, check_password
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import FoodBank, Merchant, User, Location
from listings.models import (
    Category,
    PerishableProduct,
    ListingSafety,
    ListingCommerce,
    StockLedger,
)
from listings.utils import calculate_haversine_km
from notifications.models import AdminNotification, UserNotification
from .models import Donation, DonationRequest
from .serializers import (
    DonationRequestSerializer,
    DonationSerializer,
    commerce_expires_at,
    donation_expires_at,
    donation_food_name,
    donation_unit,
    format_expiry_strings,
    merchant_primary_location,
    merchant_contact_details,
)

from accounts.governance import get_governance_config

DEFAULT_FOOD_TYPES = [
    "Bread",
    "Pastry",
    "Cake",
    "Fruits",
    "Vegetables",
    "Leafy Greens",
    "Root Crops",
    "Custom/Others",
]

OFFER_SELECT = (
    "merchant",
    "merchant__user",
    "food_bank",
    "food_bank__user",
    "listing__listing__product__category",
    "listing__listing__product__unit",
)
OFFER_PREFETCH = ("merchant__user__locations", "merchant__contacts")


class FoodBankAPIView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]


def format_radius_label(val):
    try:
        f_val = float(val)
        return int(f_val) if f_val.is_integer() else round(f_val, 1)
    except Exception:
        return 10


def resolve_foodbank(request):
    """
    Strictly resolves an authenticated Food Bank by explicit org_id (> 0) or email.
    """
    org_id = (
        request.GET.get("org_id")
        or request.data.get("org_id")
        or request.GET.get("foodbank")
        or request.data.get("foodbank")
    )
    email = request.GET.get("email") or request.data.get("email")
    if org_id and str(org_id).isdigit() and int(org_id) > 0:
        fb = (
            FoodBank.objects.select_related("user")
            .filter(org_id=int(org_id))
            .first()
        )
        if fb:
            return fb
    if email:
        fb = (
            FoodBank.objects.select_related("user")
            .filter(user__user_email__iexact=str(email).strip())
            .first()
        )
        if fb:
            return fb
    return None


def resolve_donor_merchant(request):
    """
    Strictly resolves a Merchant by explicit merchant ID (> 0) or email.
    """
    merch_id = (
        request.data.get("merchant")
        or request.data.get("merchant_id")
        or request.GET.get("merchant")
    )
    email = request.data.get("email") or request.GET.get("email")
    if merch_id and str(merch_id).isdigit() and int(merch_id) > 0:
        m = (
            Merchant.objects.select_related("user")
            .filter(merch_id=int(merch_id))
            .first()
        )
        if m:
            return m
    if email:
        m = (
            Merchant.objects.select_related("user")
            .filter(user__user_email__iexact=str(email).strip())
            .first()
        )
        if m:
            return m
    return None


def get_foodbank_coords(fb, request=None):
    if request:
        lat_q = request.GET.get("lat")
        lng_q = request.GET.get("lng")
        if lat_q and lng_q:
            try:
                return float(lat_q), float(lng_q)
            except ValueError:
                pass
    if fb and fb.user_id:
        loc = (
            Location.objects.filter(user_id=fb.user_id)
            .order_by("-is_primary", "-loc_id")
            .first()
        )
        if loc:
            return float(loc.latitude), float(loc.longitude)
    return 10.3157, 123.8854


def seconds_left(donation):
    expires = donation_expires_at(donation)
    if not expires:
        return None
    if timezone.is_naive(expires):
        expires = timezone.make_aware(expires)
    return max(0, int((expires - timezone.now()).total_seconds()))


def notify_party(user_obj, message, notif_type="Donation", merchant_obj=None):
    try:
        if user_obj:
            UserNotification.objects.create(user=user_obj, message=message)
        admin_u = (
            User.objects.filter(user_role="Admin").first()
            or User.objects.first()
        )
        if admin_u:
            AdminNotification.objects.create(
                user=admin_u,
                admin_notif_type=notif_type[:100],
                message=message,
                merchant=merchant_obj,
            )
    except Exception as e:
        print("Notification log notice:", e)


def merchant_auto_donate_enabled(merchant):
    """
    Checks if the merchant has 'autoDonateExpiry' enabled in User.settings_json (True by default).
    """
    if not merchant or not merchant.user:
        return True
    try:
        cfg = json.loads(merchant.user.settings_json or "{}")
        return bool(cfg.get("autoDonateExpiry", True))
    except Exception:
        return True


def auto_enforce_donation_safety_cutoffs():
    """
    Enterprise Lifecycle & TTT Safety Enforcer (Fixes Flaws 1 & 4):
    1. T-Minus 60m to 31m (Flaw 4): If an Active listing has unsold stock (qty_available > 0)
       and the merchant has autoDonateExpiry enabled, automatically convert remaining stock
       into an Available Donation (trigger_type='System (TTT Auto)').
    2. T-Minus 30m Cutoff (Flaw 1A): Unclaimed 'Available'/'Active' Donations within 30 mins
       of expiry are automatically set to 'Flagged'.
    3. Post-Expiry Ongoing Claims (Flaw 1B): 'Ongoing'/'Claimed' Donations past their
       estimated_expiry without a QR scan are closed as 'Expired'.
    4. Stale Appeals (Flaw 1C): 'Pending' DonationRequests whose listing is out of stock,
       Flagged, or within 30 mins of expiry are closed as 'Expired'; 'Ongoing' appeals
       past estimated_expiry are also closed as 'Expired'.
    """
    now = timezone.now()

    # 1. FLAW 4 FIX: TTT AUTO-DONATE (Between 60 mins and 30 mins before expiry)
    active_safety_qs = ListingSafety.objects.filter(
        estimated_expiry__isnull=False
    ).select_related(
        "commerce", "product__merchant__user", "product__unit"
    )

    for saf in active_safety_qs:
        comm = getattr(saf, "commerce", None)
        if not comm or comm.status != "Active":
            continue

        exp_dt = saf.estimated_expiry
        if timezone.is_naive(exp_dt):
            exp_dt = timezone.make_aware(exp_dt)

        secs = (exp_dt - now).total_seconds()
        avail_qty = int(comm.qty_available or 0)

        # If <= 30 mins left, flag the commercial listing
        if secs <= 1830:
            comm.status = "Flagged"
            comm.save(update_fields=["status"])
            continue

        # If between 30.5 mins and 60 mins left and stock > 0, check autoDonateExpiry
        if 1830 < secs <= 3600 and avail_qty > 0:
            prod = saf.product
            merch = prod.merchant if prod else None
            if merch and merchant_auto_donate_enabled(merch):
                with transaction.atomic():
                    comm.qty_available = 0
                    if int(comm.qty_reserved or 0) == 0:
                        comm.status = "Donated"
                    comm.save()

                    existing_don = Donation.objects.filter(
                        merchant=merch,
                        listing=comm,
                        status__in=["Available", "Active"],
                    ).first()
                    if existing_don:
                        existing_don.qty_donated = (
                            int(existing_don.qty_donated or 0) + avail_qty
                        )
                        existing_don.trigger_type = "System (TTT Auto)"
                        existing_don.updated_at = now
                        existing_don.save()
                    else:
                        Donation.objects.create(
                            trigger_type="System (TTT Auto)",
                            qty_donated=avail_qty,
                            status="Available",
                            merchant=merch,
                            listing=comm,
                        )

                    StockLedger.objects.create(
                        listing=comm,
                        transaction_type="TTT_Auto_Donation",
                        qty_change=-avail_qty,
                        reason=f"Auto-donated {avail_qty} units at 1-hour TTT threshold",
                    )

                unit_s = (
                    prod.unit.unit_name if (prod and prod.unit) else "units"
                )
                notify_party(
                    merch.user if merch else None,
                    f"⚡ TTT Auto-Donate: {avail_qty} {unit_s} of '{prod.prod_name}' entered the 1-hour expiry window and were automatically posted to the Food Bank Donation Pool.",
                    notif_type="Donation",
                    merchant_obj=merch,
                )

    # 2. FLAW 1A & 1B FIX: ENFORCE CUTOFFS ON OPEN & ONGOING DONATIONS
    active_donations = Donation.objects.filter(
        status__in=["Available", "Active", "Ongoing", "Claimed", "In-Transit"]
    ).select_related("merchant__user", "food_bank__user", "listing__listing__product")

    for d in active_donations:
        exp_dt = donation_expires_at(d)
        if not exp_dt:
            continue
        if timezone.is_naive(exp_dt):
            exp_dt = timezone.make_aware(exp_dt)
        secs = (exp_dt - now).total_seconds()

        if d.status in ("Available", "Active") and secs <= 1800:
            d.status = "Flagged"
            d.updated_at = now
            d.save(update_fields=["status", "updated_at"])
        elif d.status in ("Ongoing", "Claimed", "In-Transit") and secs <= 0:
            d.status = "Expired"
            d.updated_at = now
            d.save(update_fields=["status", "updated_at"])
            food_title = donation_food_name(d)
            if d.food_bank and d.food_bank.user:
                notify_party(
                    d.food_bank.user,
                    f"Pickup window expired for claimed donation DON-{d.donation_id} ({d.qty_donated}x {food_title}). Record marked as Expired for food safety compliance.",
                    merchant_obj=d.merchant,
                )

    # 3. FLAW 1C FIX: EXPIRE STALE PENDING / ONGOING FOOD BANK APPEALS
    open_requests = DonationRequest.objects.filter(
        status__in=["Pending", "Active", "Ongoing"]
    ).select_related(
        "merchant__user",
        "food_bank__user",
        "listing__listing__product",
    )

    for r in open_requests:
        comm = r.listing
        if not comm:
            continue
        exp_dt = commerce_expires_at(comm)
        if exp_dt and timezone.is_naive(exp_dt):
            exp_dt = timezone.make_aware(exp_dt)
        secs = (exp_dt - now).total_seconds() if exp_dt else 999999

        if r.status in ("Pending", "Active"):
            if (
                secs <= 1800
                or comm.status in ("Flagged", "Expired")
                or int(comm.qty_available or 0) <= 0
            ):
                r.status = "Expired"
                r.updated_at = now
                r.save(update_fields=["status", "updated_at"])
        elif r.status == "Ongoing" and secs <= 0:
            r.status = "Expired"
            r.updated_at = now
            r.save(update_fields=["status", "updated_at"])


class DonationRequestView(FoodBankAPIView):
    """
    Strictly Inventory-Backed Requests Only (Option 2):
    Every Food Bank request must select one or more active Bakeshop or Market Vendor
    listings within the governance radius that currently have available stock.
    """

    def get(self, request):
        auto_enforce_donation_safety_cutoffs()
        fb = resolve_foodbank(request)
        merch_id = request.GET.get("merchant")
        gov_radius = format_radius_label(get_governance_config()["radius_km"])

        requests_qs = (
            DonationRequest.objects.all()
            .select_related(
                "merchant__user",
                "food_bank__user",
                "listing__listing__product__category",
                "listing__listing__product__unit",
                "listing__listing__product__merchant__user",
            )
            .order_by("-donation_req_id")
        )

        if merch_id and str(merch_id).isdigit() and int(merch_id) > 0:
            m_int = int(merch_id)
            requests_qs = (
                requests_qs.filter(merchant_id=m_int)
                | requests_qs.filter(
                    listing__listing__product__merchant_id=m_int
                )
            ).distinct().order_by("-donation_req_id")
        elif fb:
            requests_qs = requests_qs.filter(food_bank=fb)
        elif request.GET.get("org_id") or request.GET.get("email"):
            return Response([], status=status.HTTP_200_OK)

        serialized = DonationRequestSerializer(requests_qs, many=True).data

        grouped_map = {}
        ordered_groups = []

        for item in serialized:
            b_code = item.get("batch_code") or f"BREQ-{item['donation_req_id']}"
            line_entry = {
                "donation_req_id": item["donation_req_id"],
                "listing_id": item["listing"],
                "food_name": item["food_name"] or item["food_type"],
                "food_type": item["food_type"],
                "qty_needed": item["qty_needed"],
                "qty_available": item["qty_available"],
                "unit_name": item["unit_name"],
                "image_path": item["image_path"],
                "expiry_date": item["expiry_date"],
                "expiry_time": item["expiry_time"],
                "expiry_display": item["expiry_display"],
                "expires_at": item["expires_at"],
                "status": item["status"],
            }
            if b_code not in grouped_map:
                grp = dict(item)
                grp["batch_code"] = b_code
                grp["voucher_code"] = b_code
                grp["qr_payload"] = f"RESERVE:{b_code}"
                grp["radius_km"] = gov_radius
                grp["items_breakdown"] = [line_entry]
                grp["total_qty"] = int(item["qty_needed"] or 0)
                grouped_map[b_code] = grp
                ordered_groups.append(grp)
            else:
                grp = grouped_map[b_code]
                grp["items_breakdown"].append(line_entry)
                grp["total_qty"] += int(item["qty_needed"] or 0)
                names = [
                    f"{x['qty_needed']}x {x['food_name']}"
                    for x in grp["items_breakdown"]
                ]
                grp["food_type"] = ", ".join(names)
                grp["food_name"] = (
                    f"{grp['items_breakdown'][0]['food_name']} +{len(grp['items_breakdown']) - 1} more"
                )
                grp["qty_needed"] = grp["total_qty"]

        return Response(ordered_groups, status=status.HTTP_200_OK)

    def post(self, request):
        auto_enforce_donation_safety_cutoffs()
        fb = resolve_foodbank(request)
        if not fb:
            return Response(
                {"error": "Food Bank account not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        items = request.data.get("items", [])
        reason = str(request.data.get("reason", "")).strip()
        additional_info = str(request.data.get("additional_info", "")).strip()
        merch_id = request.data.get("merchant_id") or request.data.get(
            "merchant"
        )
        gov_radius = format_radius_label(get_governance_config()["radius_km"])

        if not isinstance(items, list) or len(items) == 0:
            return Response(
                {
                    "error": (
                        f"Please select at least one active surplus listing from a Bakeshop or "
                        f"Market Vendor within {gov_radius}km to submit a donation appeal."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        target_merchant = None
        if merch_id and str(merch_id).isdigit() and int(merch_id) > 0:
            target_merchant = (
                Merchant.objects.select_related("user")
                .filter(merch_id=int(merch_id))
                .first()
            )

        batch_code = f"BREQ-{random.randint(10000, 99999)}"
        created_rows = []
        summary_parts = []
        now = timezone.now()

        with transaction.atomic():
            for it in items:
                qty_req = max(
                    1,
                    int(it.get("qty_needed") or it.get("quantity") or 1),
                )
                comm_id = it.get("commerce_id") or it.get("listing_id")
                prod_id = it.get("prod_id") or it.get("id")

                comm_obj = None
                if comm_id and str(comm_id).isdigit():
                    comm_obj = (
                        ListingCommerce.objects.select_related(
                            "listing__product__merchant__user",
                            "listing__product__unit",
                        )
                        .filter(pk=int(comm_id))
                        .first()
                    )
                if not comm_obj and prod_id and str(prod_id).isdigit():
                    saf = (
                        ListingSafety.objects.filter(
                            product_id=int(prod_id)
                        )
                        .select_related(
                            "commerce",
                            "product__merchant__user",
                            "product__unit",
                        )
                        .first()
                    )
                    comm_obj = getattr(saf, "commerce", None) if saf else None

                if (
                    not comm_obj
                    or comm_obj.status != "Active"
                    or int(comm_obj.qty_available or 0) <= 0
                ):
                    continue

                saf_rec = comm_obj.listing
                if saf_rec and saf_rec.estimated_expiry:
                    exp_dt = saf_rec.estimated_expiry
                    if timezone.is_naive(exp_dt):
                        exp_dt = timezone.make_aware(exp_dt)
                    if (exp_dt - now).total_seconds() <= 1800:
                        continue

                clamped_qty = min(int(comm_obj.qty_available or 1), qty_req)
                prod_obj = saf_rec.product if saf_rec else None
                food_label = (
                    prod_obj.prod_name
                    if prod_obj
                    else str(it.get("food_name") or "Surplus Item")
                )
                row_merch = target_merchant or (
                    prod_obj.merchant if prod_obj else None
                )

                req_row = DonationRequest.objects.create(
                    batch_code=batch_code,
                    food_type=food_label,
                    qty_needed=clamped_qty,
                    reason=reason or "Community food relief distribution",
                    additional_info=additional_info,
                    status="Pending",
                    merchant=row_merch,
                    food_bank=fb,
                    listing=comm_obj,
                )
                created_rows.append(req_row)
                summary_parts.append(f"{clamped_qty}x {food_label}")

        if not created_rows:
            return Response(
                {
                    "error": (
                        "The selected items are no longer available or have entered the "
                        "30-minute safety cutoff. Please refresh and select active listings."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        first_m = created_rows[0].merchant
        if first_m and first_m.user:
            notify_party(
                first_m.user,
                f"New Donation Appeal ({batch_code}) from {fb.org_name}: {', '.join(summary_parts)}. Reason: {reason or 'Community relief'}.",
                notif_type="Donation",
                merchant_obj=first_m,
            )
        notify_party(
            fb.user,
            f"Your donation appeal {batch_code} ({', '.join(summary_parts)}) was sent to {first_m.bus_name if first_m else 'Merchant'}.",
            notif_type="Donation",
        )

        return Response(
            {
                "message": f"Donation appeal ({batch_code}) sent to {first_m.bus_name if first_m else 'merchant'}!",
                "batch_code": batch_code,
                "radius_km": gov_radius,
                "requests": DonationRequestSerializer(
                    created_rows, many=True
                ).data,
            },
            status=status.HTTP_201_CREATED,
        )

    def patch(self, request):
        auto_enforce_donation_safety_cutoffs()
        req_id = request.data.get("donation_req_id")
        batch_code = request.data.get("batch_code")
        action = str(request.data.get("action", "")).strip().lower()
        new_status = str(request.data.get("status", "")).strip()
        edited_items = request.data.get("items", [])
        merch_id = request.data.get("merchant")

        qs = DonationRequest.objects.select_related(
            "merchant__user",
            "food_bank__user",
            "listing__listing__product__merchant__user",
        )
        if batch_code:
            target_rows = list(qs.filter(batch_code=batch_code))
        elif req_id:
            first_row = qs.filter(donation_req_id=req_id).first()
            if first_row and first_row.batch_code:
                target_rows = list(
                    qs.filter(batch_code=first_row.batch_code)
                )
            elif first_row:
                target_rows = [first_row]
            else:
                target_rows = []
        else:
            target_rows = []

        if not target_rows:
            return Response(
                {"error": "Donation request not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        merch_obj = None
        if merch_id and str(merch_id).isdigit() and int(merch_id) > 0:
            merch_obj = (
                Merchant.objects.select_related("user")
                .filter(merch_id=int(merch_id))
                .first()
            )
        if not merch_obj:
            merch_obj = target_rows[0].merchant or (
                target_rows[0].listing.listing.product.merchant
                if (
                    target_rows[0].listing
                    and target_rows[0].listing.listing
                    and target_rows[0].listing.listing.product
                )
                else None
            )

        fb_obj = target_rows[0].food_bank
        b_label = (
            target_rows[0].batch_code
            or f"BREQ-{target_rows[0].donation_req_id}"
        )

        qty_override_map = {}
        if isinstance(edited_items, list):
            for entry in edited_items:
                rid = entry.get("donation_req_id")
                qval = (
                    entry.get("qty_needed")
                    or entry.get("qty_approved")
                    or entry.get("quantity")
                )
                if rid and qval is not None:
                    try:
                        qty_override_map[int(rid)] = max(1, int(qval))
                    except (ValueError, TypeError):
                        pass

        single_qty_override = request.data.get(
            "qty_approved"
        ) or request.data.get("qty_needed")

        now = timezone.now()

        with transaction.atomic():
            if action == "withdraw" or new_status in (
                "Withdrawn",
                "Cancelled",
            ):
                for r in target_rows:
                    if r.status == "Ongoing" and r.listing:
                        r.listing.qty_available = int(
                            r.listing.qty_available or 0
                        ) + int(r.qty_needed or 0)
                        if r.listing.status == "Donated":
                            r.listing.status = "Active"
                        r.listing.save()
                    r.status = "Withdrawn"
                    r.updated_at = now
                    r.save()
                if merch_obj and merch_obj.user:
                    notify_party(
                        merch_obj.user,
                        f"{fb_obj.org_name if fb_obj else 'Food Bank'} withdrew donation appeal {b_label}.",
                        merchant_obj=merch_obj,
                    )
                return Response(
                    {
                        "message": f"Appeal {b_label} has been withdrawn.",
                        "status": "Withdrawn",
                    }
                )

            elif action == "reject" or new_status == "Rejected":
                for r in target_rows:
                    r.status = "Rejected"
                    if merch_obj:
                        r.merchant = merch_obj
                    r.updated_at = now
                    r.save()
                if fb_obj and fb_obj.user:
                    notify_party(
                        fb_obj.user,
                        f"Your donation appeal {b_label} was declined by {merch_obj.bus_name if merch_obj else 'the merchant'}.",
                        merchant_obj=merch_obj,
                    )
                return Response(
                    {
                        "message": f"Appeal {b_label} marked as Rejected.",
                        "status": "Rejected",
                    }
                )

            # FLAW 2 FIX: STRICT STOCK & SAFETY CUTOFF VERIFICATION BEFORE APPROVAL
            elif action == "approve" or new_status == "Ongoing":
                approved_summary = []
                out_of_stock_items = []

                for r in target_rows:
                    if r.donation_req_id in qty_override_map:
                        r.qty_needed = qty_override_map[r.donation_req_id]
                    elif (
                        single_qty_override is not None
                        and len(target_rows) == 1
                    ):
                        try:
                            r.qty_needed = max(1, int(single_qty_override))
                        except (ValueError, TypeError):
                            pass

                    if r.listing:
                        comm = r.listing
                        exp_dt = commerce_expires_at(comm)
                        if exp_dt and timezone.is_naive(exp_dt):
                            exp_dt = timezone.make_aware(exp_dt)
                        if exp_dt and (exp_dt - now).total_seconds() <= 1800:
                            r.status = "Expired"
                            r.updated_at = now
                            r.save()
                            out_of_stock_items.append(
                                f"{r.food_type} (within 30-min safety cutoff)"
                            )
                            continue

                        avail_stock = int(comm.qty_available or 0)
                        if comm.status != "Active" or avail_stock <= 0:
                            r.status = "Rejected"
                            r.updated_at = now
                            r.save()
                            out_of_stock_items.append(
                                f"{r.food_type} (0 stock remaining)"
                            )
                            continue

                        deduct = min(avail_stock, int(r.qty_needed or 1))
                        comm.qty_available = max(0, avail_stock - deduct)
                        if (
                            comm.qty_available == 0
                            and int(comm.qty_reserved or 0) == 0
                        ):
                            comm.status = "Donated"
                        comm.save()
                        r.qty_needed = deduct

                    if merch_obj:
                        r.merchant = merch_obj
                    r.status = "Ongoing"
                    r.updated_at = now
                    r.save()
                    approved_summary.append(f"{r.qty_needed}x {r.food_type}")

                if not approved_summary:
                    return Response(
                        {
                            "error": (
                                f"Cannot approve appeal {b_label}: "
                                f"{', '.join(out_of_stock_items) or 'Requested items are out of stock or past the 30-minute safety cutoff'}."
                            )
                        },
                        status=status.HTTP_400_BAD_REQUEST,
                    )

                if fb_obj and fb_obj.user:
                    notify_party(
                        fb_obj.user,
                        f"Appeal {b_label} APPROVED by {merch_obj.bus_name if merch_obj else 'Merchant'} ({', '.join(approved_summary)})! Your QR Pickup Voucher ({b_label}) is now active.",
                        merchant_obj=merch_obj,
                    )
                return Response(
                    {
                        "message": f"Appeal {b_label} approved ({', '.join(approved_summary)})! Status is now Ongoing and QR Voucher is active.",
                        "status": "Ongoing",
                        "voucher_code": b_label,
                    }
                )

            elif action in (
                "complete",
                "fulfill",
                "successful",
            ) or new_status in ("Successful", "Fulfilled", "Completed"):
                for r in target_rows:
                    if r.listing:
                        exp_dt = commerce_expires_at(r.listing)
                        if exp_dt and timezone.is_naive(exp_dt):
                            exp_dt = timezone.make_aware(exp_dt)
                        if exp_dt and exp_dt <= now:
                            r.status = "Expired"
                            r.updated_at = now
                            r.save()
                            return Response(
                                {
                                    "error": f"Cannot mark appeal {b_label} as Successful because '{r.food_type}' has passed its safety expiry timestamp."
                                },
                                status=status.HTTP_400_BAD_REQUEST,
                            )

                total_units = Decimal("0.00")
                completed_summary = []
                for r in target_rows:
                    if r.status == "Successful":
                        continue
                    if merch_obj:
                        r.merchant = merch_obj
                    r.status = "Successful"
                    r.updated_at = now
                    r.save()
                    total_units += Decimal(str(r.qty_needed or 1))
                    completed_summary.append(f"{r.qty_needed}x {r.food_type}")

                    if r.listing:
                        StockLedger.objects.create(
                            listing=r.listing,
                            transaction_type="Donation_Handoff",
                            qty_change=-int(r.qty_needed or 1),
                            reason=f"Fulfilled Food Bank appeal {b_label} for {fb_obj.org_name if fb_obj else 'Food Bank'}",
                        )

                if fb_obj and total_units > Decimal("0.00"):
                    fb_obj.total_food_received = (
                        Decimal(str(fb_obj.total_food_received or 0))
                        + total_units
                    )
                    fb_obj.save(update_fields=["total_food_received"])
                    if fb_obj.user:
                        notify_party(
                            fb_obj.user,
                            f"QR Handshake verified! Donation appeal {b_label} ({', '.join(completed_summary)}) marked as Successful!",
                            merchant_obj=merch_obj,
                        )
                if merch_obj and merch_obj.user and completed_summary:
                    notify_party(
                        merch_obj.user,
                        f"Donation appeal {b_label} ({', '.join(completed_summary)}) with {fb_obj.org_name if fb_obj else 'Food Bank'} is now Successful!",
                        merchant_obj=merch_obj,
                    )
                return Response(
                    {
                        "message": f"Transaction {b_label} verified and marked as Successful!",
                        "status": "Successful",
                    }
                )

        return Response(DonationRequestSerializer(target_rows[0]).data)


class FoodTypesView(FoodBankAPIView):
    def get(self, request):
        names = list(
            Category.objects.order_by("category_name").values_list(
                "category_name", flat=True
            )
        )
        gov_radius = format_radius_label(get_governance_config()["radius_km"])
        return Response(
            {
                "food_types": names or DEFAULT_FOOD_TYPES,
                "radius_km": gov_radius,
            }
        )


class AvailableDonationsView(FoodBankAPIView):
    """
    GET: Returns posted donations within the governance Haversine radius of the Food Bank.
    POST: Allows an authenticated merchant to post single OR multiple active listings for donation.
    """

    def get(self, request):
        auto_enforce_donation_safety_cutoffs()
        fb = resolve_foodbank(request)
        fb_lat, fb_lng = get_foodbank_coords(fb, request)
        gov_radius = float(get_governance_config()["radius_km"])
        max_radius = float(request.GET.get("radius") or gov_radius)
        radius_label = format_radius_label(max_radius)
        merch_filter = request.GET.get("merchant")

        donations_qs = (
            Donation.objects.all()
            .select_related(*OFFER_SELECT)
            .prefetch_related(*OFFER_PREFETCH)
            .order_by("-donation_id")
        )
        if merch_filter and str(merch_filter).isdigit() and int(merch_filter) > 0:
            donations_qs = donations_qs.filter(merchant_id=int(merch_filter))
        elif fb:
            donations_qs = donations_qs.filter(
                status__in=["Available", "Active"]
            ) | donations_qs.filter(food_bank=fb)
            donations_qs = donations_qs.distinct().order_by("-donation_id")

        results = []
        for d in donations_qs:
            serialized = DonationSerializer(d).data
            m_lat = float(serialized.get("merchant_lat") or 10.3157)
            m_lng = float(serialized.get("merchant_lng") or 123.8854)
            dist_km = calculate_haversine_km(fb_lat, fb_lng, m_lat, m_lng)
            serialized["distance_km"] = dist_km
            serialized["distance"] = f"{dist_km:.1f} km"
            serialized["radius_km"] = radius_label

            if (
                not merch_filter
                and dist_km > max_radius
                and d.status in ("Active", "Available")
            ):
                continue
            results.append(serialized)

        return Response(results, status=status.HTTP_200_OK)

    def post(self, request):
        merchant = resolve_donor_merchant(request)
        if not merchant:
            return Response(
                {"error": "Authenticated merchant account not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        items = request.data.get("items", [])
        if not items and request.data.get("listing"):
            items = [
                {
                    "listing": request.data.get("listing"),
                    "qty_donated": request.data.get("qty_donated", 1),
                }
            ]

        if not items:
            return Response(
                {"error": "Please select at least one active listing to donate."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        created_donations = []
        posted_summary = []

        with transaction.atomic():
            for it in items:
                raw_listing_id = (
                    it.get("commerce_id") or it.get("listing") or it.get("id")
                )
                qty_to_donate = max(
                    1, int(it.get("qty_donated") or it.get("quantity") or 1)
                )

                comm_obj = None
                if raw_listing_id and str(raw_listing_id).isdigit():
                    comm_obj = (
                        ListingCommerce.objects.select_related(
                            "listing__product"
                        )
                        .filter(pk=int(raw_listing_id))
                        .first()
                    )
                    if not comm_obj:
                        saf = (
                            ListingSafety.objects.filter(
                                product_id=int(raw_listing_id)
                            )
                            .select_related("commerce", "product")
                            .first()
                        )
                        comm_obj = (
                            getattr(saf, "commerce", None) if saf else None
                        )

                if not comm_obj:
                    continue

                avail = int(comm_obj.qty_available or 0)
                if avail <= 0:
                    continue
                actual_donate_qty = min(avail, qty_to_donate)

                comm_obj.qty_available = max(0, avail - actual_donate_qty)
                if (
                    comm_obj.qty_available == 0
                    and (comm_obj.qty_reserved or 0) == 0
                ):
                    comm_obj.status = "Donated"
                comm_obj.save()

                don_obj = Donation.objects.create(
                    trigger_type="Manual",
                    qty_donated=actual_donate_qty,
                    status="Available",
                    merchant=merchant,
                    listing=comm_obj,
                )
                created_donations.append(don_obj)
                p_name = (
                    comm_obj.listing.product.prod_name
                    if (comm_obj.listing and comm_obj.listing.product)
                    else "Surplus Item"
                )
                posted_summary.append(f"{actual_donate_qty}x {p_name}")

        if not created_donations:
            return Response(
                {
                    "error": "Could not post donations. Please verify stock availability."
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        if merchant and merchant.user:
            notify_party(
                merchant.user,
                f"Posted {len(created_donations)} surplus donation(s) to Food Banks: {', '.join(posted_summary)}.",
                merchant_obj=merchant,
            )

        return Response(
            {
                "message": f"Successfully posted {len(created_donations)} donation(s) ({', '.join(posted_summary)}) for Food Banks!",
                "donations": DonationSerializer(
                    created_donations, many=True
                ).data,
            },
            status=status.HTTP_201_CREATED,
        )


class DonationRespondView(FoodBankAPIView):
    """
    Handles:
    - Partial or Full Claiming by a Food Bank (`action in ('accept', 'appeal', 'claim')` with optional `claimed_qty`)
    - Early Claim Release by a Food Bank (`action == 'release_claim'`), returning stock to the open donation pool
    - Merchant Cancellation (`action == 'cancel'`), restoring stock to active consumer inventory
    - Handshake Completion (`action in ('received', 'complete', 'successful')`)
    """

    def patch(self, request, donation_id):
        auto_enforce_donation_safety_cutoffs()
        fb = resolve_foodbank(request)
        action = str(request.data.get("action", "")).strip().lower()
        qty_override = request.data.get("qty_donated") or request.data.get(
            "claimed_qty"
        )
        now = timezone.now()

        with transaction.atomic():
            donation = (
                Donation.objects.select_for_update()
                .select_related(
                    "merchant__user",
                    "food_bank__user",
                    "listing__listing__product",
                )
                .filter(donation_id=donation_id)
                .first()
            )
            if not donation:
                return Response(
                    {"error": "Donation not found."},
                    status=status.HTTP_404_NOT_FOUND,
                )

            food_title = donation_food_name(donation)
            unit_label = donation_unit(donation) or "items"
            exp_dt = donation_expires_at(donation)
            if exp_dt and timezone.is_naive(exp_dt):
                exp_dt = timezone.make_aware(exp_dt)

            if action == "update_qty" and qty_override is not None:
                donation.qty_donated = max(1, int(qty_override))
                donation.updated_at = now
                donation.save()

            elif action in ("accept", "appeal", "claim"):
                if not fb:
                    return Response(
                        {
                            "error": "Authenticated Food Bank account required to claim donations."
                        },
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                if exp_dt and (exp_dt - now).total_seconds() <= 1800:
                    donation.status = "Flagged"
                    donation.updated_at = now
                    donation.save()
                    return Response(
                        {
                            "error": f"Cannot claim '{food_title}' because it has entered the 30-minute food safety cutoff."
                        },
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                if donation.status not in ("Available", "Active"):
                    return Response(
                        {
                            "error": f"This donation is no longer available (Status: {donation.status})."
                        },
                        status=status.HTTP_400_BAD_REQUEST,
                    )

                total_pool_qty = int(donation.qty_donated or 1)
                requested_claim_qty = total_pool_qty
                if qty_override is not None:
                    try:
                        requested_claim_qty = max(
                            1, min(total_pool_qty, int(qty_override))
                        )
                    except (ValueError, TypeError):
                        requested_claim_qty = total_pool_qty

                if requested_claim_qty < total_pool_qty:
                    donation.qty_donated = total_pool_qty - requested_claim_qty
                    donation.updated_at = now
                    donation.save()

                    claimed_donation = Donation.objects.create(
                        trigger_type=donation.trigger_type,
                        qty_donated=requested_claim_qty,
                        status="Ongoing",
                        merchant=donation.merchant,
                        food_bank=fb,
                        listing=donation.listing,
                    )
                    target_don = claimed_donation
                else:
                    donation.food_bank = fb
                    donation.status = "Ongoing"
                    donation.updated_at = now
                    donation.save()
                    target_don = donation

                if donation.merchant and donation.merchant.user:
                    notify_party(
                        donation.merchant.user,
                        (
                            f"{fb.org_name} claimed {target_don.qty_donated} {unit_label} of {food_title} "
                            f"(Voucher DON-{target_don.donation_id})! Status is now Ongoing."
                        ),
                        merchant_obj=donation.merchant,
                    )
                if fb.user:
                    notify_party(
                        fb.user,
                        (
                            f"You claimed {target_don.qty_donated} {unit_label} of {food_title} from "
                            f"{donation.merchant.bus_name}. Present QR Voucher DON-{target_don.donation_id} at pickup!"
                        ),
                        merchant_obj=donation.merchant,
                    )
                return Response(DonationSerializer(target_don).data)

            elif action == "release_claim":
                if donation.status not in ("Ongoing", "Claimed", "In-Transit"):
                    return Response(
                        {"error": "Only ongoing claims can be released."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                released_qty = int(donation.qty_donated or 1)
                existing_pool = (
                    Donation.objects.filter(
                        merchant=donation.merchant,
                        listing=donation.listing,
                        status__in=["Available", "Active"],
                    )
                    .exclude(donation_id=donation.donation_id)
                    .first()
                )
                if existing_pool:
                    existing_pool.qty_donated = (
                        int(existing_pool.qty_donated or 0) + released_qty
                    )
                    existing_pool.updated_at = now
                    existing_pool.save()
                    donation.status = "Cancelled"
                    donation.updated_at = now
                    donation.save()
                else:
                    donation.food_bank = None
                    donation.status = (
                        "Flagged"
                        if (exp_dt and (exp_dt - now).total_seconds() <= 1800)
                        else "Available"
                    )
                    donation.updated_at = now
                    donation.save()

                if donation.merchant and donation.merchant.user:
                    notify_party(
                        donation.merchant.user,
                        f"{fb.org_name if fb else 'Food Bank'} released their claim on {released_qty} {unit_label} of {food_title}.",
                        merchant_obj=donation.merchant,
                    )
                return Response(DonationSerializer(donation).data)

            elif action in ("decline", "cancel"):
                if (
                    donation.status in ("Active", "Available", "Ongoing")
                    and donation.listing
                ):
                    donation.listing.qty_available = int(
                        donation.listing.qty_available or 0
                    ) + int(donation.qty_donated or 0)
                    if donation.listing.status == "Donated":
                        donation.listing.status = "Active"
                    donation.listing.save()
                donation.status = (
                    "Cancelled" if action == "cancel" else "Declined"
                )
                donation.updated_at = now
                donation.save()

            elif action in ("received", "complete", "successful"):
                if exp_dt and exp_dt <= now:
                    donation.status = "Expired"
                    donation.updated_at = now
                    donation.save()
                    return Response(
                        {
                            "error": f"Safety Cutoff Reached: '{food_title}' passed its safety expiry window and cannot be marked as Successful."
                        },
                        status=status.HTTP_400_BAD_REQUEST,
                    )

                if donation.status != "Successful":
                    donation.status = "Successful"
                    donation.updated_at = now
                    donation.save()

                    if donation.listing:
                        StockLedger.objects.create(
                            listing=donation.listing,
                            transaction_type="Donation_Handoff",
                            qty_change=-int(donation.qty_donated or 1),
                            reason=f"Completed donation DON-{donation.donation_id} to {donation.food_bank.org_name if donation.food_bank else 'Food Bank'}",
                        )

                    target_fb = donation.food_bank or fb
                    if target_fb:
                        target_fb.total_food_received = Decimal(
                            str(target_fb.total_food_received or 0)
                        ) + Decimal(str(donation.qty_donated or 1))
                        target_fb.save(update_fields=["total_food_received"])
                        if target_fb.user:
                            notify_party(
                                target_fb.user,
                                f"Donation DON-{donation.donation_id} ({donation.qty_donated}x {food_title}) from {donation.merchant.bus_name} marked as Successful!",
                                merchant_obj=donation.merchant,
                            )
                    if donation.merchant and donation.merchant.user:
                        notify_party(
                            donation.merchant.user,
                            f"Donation DON-{donation.donation_id} ({donation.qty_donated}x {food_title}) to {target_fb.org_name if target_fb else 'Food Bank'} completed and marked as Successful!",
                            merchant_obj=donation.merchant,
                        )

        return Response(DonationSerializer(donation).data)


class DonationQRVerifyView(FoodBankAPIView):
    """
    POST /api/donations/verify-qr/
    Verifies a Food Bank's QR Donation Voucher (DON-<id>, BREQ-<code>, or REQ-<code>)
    scanned by a Merchant in establishment/donation.tsx, with strict post-expiry safety guards (Flaw 3).
    """

    def _clean_voucher(self, raw_str):
        cleaned = (
            str(raw_str or "")
            .replace("RESERVE:", "")
            .replace("reserve:", "")
            .strip()
            .upper()
        )
        m = re.search(r"(DON-\d+|BREQ-\d+|REQ-\d+)", cleaned)
        if m:
            return m.group(1)
        if cleaned.isdigit():
            return f"DON-{cleaned}"
        return cleaned

    def post(self, request):
        auto_enforce_donation_safety_cutoffs()
        action = str(request.data.get("action", "preview")).strip().lower()
        code = self._clean_voucher(
            request.data.get("qr_code")
            or request.data.get("voucher_code")
            or request.data.get("code")
        )
        merchant = resolve_donor_merchant(request)
        now = timezone.now()

        if not code:
            return Response(
                {
                    "error": "Unrecognized QR Voucher. Please scan a valid DON-XXX or BREQ-XXXXX voucher."
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # 1. POSTED SURPLUS DONATION VOUCHER (DON-<id>)
        if code.startswith("DON-"):
            don_id_str = code.replace("DON-", "").strip()
            if not don_id_str.isdigit():
                return Response(
                    {"error": f"Invalid Donation Voucher ID '{code}'."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            don_qs = Donation.objects.select_related(
                "merchant__user",
                "food_bank__user",
                "listing__listing__product__unit",
            ).filter(donation_id=int(don_id_str))

            if merchant:
                don_qs = don_qs.filter(merchant=merchant)

            donation = don_qs.first()
            if not donation:
                return Response(
                    {
                        "error": f"No donation record at your store matches voucher '{code}'."
                    },
                    status=status.HTTP_404_NOT_FOUND,
                )

            if donation.status == "Successful":
                return Response(
                    {
                        "error": f"Voucher {code} has already been verified and marked as Successful."
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

            # FLAW 3 FIX: BLOCK HANDOFF IF SAFETY EXPIRY HAS PASSED
            exp_dt = donation_expires_at(donation)
            if exp_dt:
                if timezone.is_naive(exp_dt):
                    exp_dt = timezone.make_aware(exp_dt)
                if exp_dt <= now:
                    donation.status = "Expired"
                    donation.updated_at = now
                    donation.save(update_fields=["status", "updated_at"])
                    exp_fmt = format_expiry_strings(exp_dt)[2]
                    return Response(
                        {
                            "error": (
                                f"Food Safety Cutoff (RA 10611): Voucher {code} expired at {exp_fmt} "
                                f"and can no longer be released."
                            )
                        },
                        status=status.HTTP_400_BAD_REQUEST,
                    )

            if donation.status in ("Flagged", "Expired", "Cancelled", "Declined"):
                return Response(
                    {
                        "error": f"Voucher {code} is {donation.status} and cannot be released."
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

            serialized = DonationSerializer(donation).data
            summary_payload = {
                "record_type": "donation",
                "voucher_code": code,
                "donation_id": donation.donation_id,
                "status": donation.status,
                "food_bank_name": serialized.get("food_bank_name")
                or "Partner Food Bank",
                "food_bank_contact_person": serialized.get(
                    "food_bank_contact_person"
                )
                or "NGO Coordinator",
                "food_bank_phone": serialized.get("food_bank_phone") or "N/A",
                "food_bank_address": serialized.get("food_bank_address")
                or "Cebu City",
                "expiry_display": serialized.get("expiry_display")
                or "Standard Window",
                "total_qty": f"{donation.qty_donated} {serialized.get('unit_name') or 'items'}",
                "items_breakdown": [
                    {
                        "name": serialized.get("food_name") or "Surplus Food",
                        "qty": donation.qty_donated,
                        "unit": serialized.get("unit_name") or "items",
                    }
                ],
                "image_path": serialized.get("image_path"),
            }

            if action == "preview":
                return Response(
                    {"handoff": summary_payload}, status=status.HTTP_200_OK
                )

            with transaction.atomic():
                donation.status = "Successful"
                donation.updated_at = now
                donation.save()

                if donation.listing:
                    StockLedger.objects.create(
                        listing=donation.listing,
                        transaction_type="Donation_QR_Handoff",
                        qty_change=-int(donation.qty_donated or 1),
                        reason=f"QR-verified donation handoff {code} to {summary_payload['food_bank_name']}",
                    )

                if donation.food_bank:
                    donation.food_bank.total_food_received = Decimal(
                        str(donation.food_bank.total_food_received or 0)
                    ) + Decimal(str(donation.qty_donated or 1))
                    donation.food_bank.save(
                        update_fields=["total_food_received"]
                    )
                    if donation.food_bank.user:
                        notify_party(
                            donation.food_bank.user,
                            f"QR Voucher {code} scanned & verified at {donation.merchant.bus_name}! {summary_payload['total_qty']} of {summary_payload['items_breakdown'][0]['name']} marked as Successful.",
                            merchant_obj=donation.merchant,
                        )

                if donation.merchant and donation.merchant.user:
                    notify_party(
                        donation.merchant.user,
                        f"QR Handoff {code} completed with {summary_payload['food_bank_name']} ({summary_payload['total_qty']}).",
                        merchant_obj=donation.merchant,
                    )

            summary_payload["status"] = "Successful"
            return Response(
                {
                    "message": f"QR Voucher {code} verified! Donation handoff to {summary_payload['food_bank_name']} is now Successful.",
                    "handoff": summary_payload,
                },
                status=status.HTTP_200_OK,
            )

        # 2. FOOD BANK APPEAL / BULK REQUEST VOUCHER (BREQ-XXXXX or REQ-XXXXX)
        req_qs = DonationRequest.objects.select_related(
            "merchant__user",
            "food_bank__user",
            "listing__listing__product__unit",
            "listing__listing__product__merchant__user",
        )
        if code.startswith("REQ-") and code.replace("REQ-", "").isdigit():
            req_id_int = int(code.replace("REQ-", ""))
            target_rows = list(
                req_qs.filter(batch_code=code)
                | req_qs.filter(donation_req_id=req_id_int)
            )
        else:
            target_rows = list(req_qs.filter(batch_code__iexact=code))

        if merchant:
            target_rows = [
                r
                for r in target_rows
                if (r.merchant_id == merchant.merch_id)
                or (
                    r.listing
                    and r.listing.listing
                    and r.listing.listing.product
                    and r.listing.listing.product.merchant_id
                    == merchant.merch_id
                )
                or (r.merchant_id is None)
            ]

        if not target_rows:
            return Response(
                {
                    "error": f"No Food Bank appeal at your store matches voucher '{code}'."
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        first_r = target_rows[0]
        if all(r.status == "Successful" for r in target_rows):
            return Response(
                {
                    "error": f"Appeal Voucher {code} has already been verified and completed."
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # FLAW 3 FIX: BLOCK APPEAL QR HANDOFF IF ANY LINKED ITEM HAS EXPIRED
        for r in target_rows:
            if r.listing:
                exp_dt = commerce_expires_at(r.listing)
                if exp_dt:
                    if timezone.is_naive(exp_dt):
                        exp_dt = timezone.make_aware(exp_dt)
                    if exp_dt <= now:
                        r.status = "Expired"
                        r.updated_at = now
                        r.save(update_fields=["status", "updated_at"])
                        exp_fmt = format_expiry_strings(exp_dt)[2]
                        return Response(
                            {
                                "error": (
                                    f"Food Safety Cutoff (RA 10611): Item '{r.food_type}' in voucher {code} "
                                    f"expired at {exp_fmt} and can no longer be released."
                                )
                            },
                            status=status.HTTP_400_BAD_REQUEST,
                        )

        if first_r.status in ("Rejected", "Withdrawn", "Expired", "Cancelled"):
            return Response(
                {
                    "error": f"Appeal Voucher {code} is {first_r.status} and cannot be released."
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        fb_obj = first_r.food_bank
        serialized_first = DonationRequestSerializer(first_r).data
        items_list = []
        total_units_int = 0
        for r in target_rows:
            sr = DonationRequestSerializer(r).data
            q_val = int(r.qty_needed or 1)
            total_units_int += q_val
            items_list.append(
                {
                    "name": sr.get("food_name") or r.food_type,
                    "qty": q_val,
                    "unit": sr.get("unit_name") or "items",
                }
            )

        summary_payload = {
            "record_type": "appeal",
            "voucher_code": first_r.batch_code
            or f"BREQ-{first_r.donation_req_id}",
            "batch_code": first_r.batch_code
            or f"BREQ-{first_r.donation_req_id}",
            "donation_req_id": first_r.donation_req_id,
            "status": first_r.status,
            "food_bank_name": serialized_first.get("food_bank_name")
            or "Partner Food Bank",
            "food_bank_contact_person": serialized_first.get(
                "food_bank_contact_person"
            )
            or "NGO Coordinator",
            "food_bank_phone": serialized_first.get("food_bank_phone") or "N/A",
            "food_bank_address": serialized_first.get("food_bank_address")
            or "Cebu City",
            "expiry_display": serialized_first.get("expiry_display")
            or "Standard Window",
            "total_qty": f"{total_units_int} units ({len(items_list)} item{'s' if len(items_list) > 1 else ''})",
            "items_breakdown": items_list,
            "image_path": serialized_first.get("image_path"),
        }

        if action == "preview":
            return Response(
                {"handoff": summary_payload}, status=status.HTTP_200_OK
            )

        with transaction.atomic():
            total_units_dec = Decimal("0.00")
            for r in target_rows:
                if r.status == "Successful":
                    continue
                if r.status in ("Pending", "Active") and r.listing:
                    avail = int(r.listing.qty_available or 0)
                    if avail <= 0:
                        return Response(
                            {
                                "error": f"Cannot complete unapproved appeal {code} because '{r.food_type}' is out of stock."
                            },
                            status=status.HTTP_400_BAD_REQUEST,
                        )
                    deduct = min(avail, int(r.qty_needed or 1))
                    r.listing.qty_available = max(0, avail - deduct)
                    r.qty_needed = deduct
                    r.listing.save()

                if merchant and not r.merchant:
                    r.merchant = merchant
                r.status = "Successful"
                r.updated_at = now
                r.save()
                total_units_dec += Decimal(str(r.qty_needed or 1))

                if r.listing:
                    StockLedger.objects.create(
                        listing=r.listing,
                        transaction_type="Donation_QR_Handoff",
                        qty_change=-int(r.qty_needed or 1),
                        reason=f"QR-verified appeal handoff {summary_payload['voucher_code']} for {summary_payload['food_bank_name']}",
                    )

            if fb_obj and total_units_dec > Decimal("0.00"):
                fb_obj.total_food_received = (
                    Decimal(str(fb_obj.total_food_received or 0))
                    + total_units_dec
                )
                fb_obj.save(update_fields=["total_food_received"])
                if fb_obj.user:
                    notify_party(
                        fb_obj.user,
                        f"QR Appeal Voucher {summary_payload['voucher_code']} verified! Handoff marked as Successful.",
                        merchant_obj=merchant,
                    )

            if merchant and merchant.user:
                notify_party(
                    merchant.user,
                    f"QR Appeal Handoff {summary_payload['voucher_code']} completed with {summary_payload['food_bank_name']}!",
                    merchant_obj=merchant,
                )

        summary_payload["status"] = "Successful"
        return Response(
            {
                "message": f"QR Voucher {summary_payload['voucher_code']} verified! Appeal handoff to {summary_payload['food_bank_name']} is now Successful.",
                "handoff": summary_payload,
            },
            status=status.HTTP_200_OK,
        )


def build_activity(fb=None, limit=100):
    """
    Builds role-specific notifications & activity strictly sorted by time (latest at index 0) with target_route.
    """
    if not fb:
        return []

    events = []
    req_qs = DonationRequest.objects.filter(food_bank=fb).select_related(
        "merchant"
    )

    for r in req_qs:
        m_name = r.merchant.bus_name if r.merchant else "Partner Merchant"
        b_code = r.batch_code or f"BREQ-{r.donation_req_id}"
        ev_time = r.updated_at or r.created_at
        if r.status in ("Ongoing", "Approved"):
            events.append(
                {
                    "id": f"req-{r.donation_req_id}-ongoing",
                    "type": "request",
                    "title": f"Request Approved — QR Ready ({b_code})",
                    "detail": f"{m_name} approved {r.qty_needed}x {r.food_type}. Present QR Voucher {b_code} at pickup.",
                    "time": ev_time,
                    "target_route": "/foodbank/requests",
                    "is_read": False,
                }
            )
        elif r.status in ("Successful", "Fulfilled"):
            events.append(
                {
                    "id": f"req-{r.donation_req_id}-success",
                    "type": "request",
                    "title": f"QR Handshake Completed ({b_code})",
                    "detail": f"Received {r.qty_needed}x {r.food_type} from {m_name}.",
                    "time": ev_time,
                    "target_route": "/foodbank/requests",
                    "is_read": False,
                }
            )
        elif r.status in ("Rejected", "Expired"):
            events.append(
                {
                    "id": f"req-{r.donation_req_id}-{r.status.lower()}",
                    "type": "request",
                    "title": f"Request {r.status} ({b_code})",
                    "detail": f"{r.qty_needed}x {r.food_type} at {m_name} was marked {r.status}.",
                    "time": ev_time,
                    "target_route": "/foodbank/requests",
                    "is_read": False,
                }
            )
        else:
            events.append(
                {
                    "id": f"req-{r.donation_req_id}-{r.status.lower()}",
                    "type": "request",
                    "title": f"Request ({r.status}): {r.food_type}",
                    "detail": f"{r.qty_needed} items requested from {m_name} · Status: {r.status}",
                    "time": r.created_at,
                    "target_route": "/foodbank/requests",
                    "is_read": True,
                }
            )

    for d in Donation.objects.all().select_related(
        "merchant", "merchant__user", "food_bank"
    ):
        if (
            d.food_bank
            and d.food_bank.org_id != fb.org_id
            and d.status not in ("Active", "Available")
        ):
            continue

        merchant_name = (
            d.merchant.bus_name if d.merchant else "Partner Merchant"
        )
        unit_str = donation_unit(d) or "items"
        food_str = donation_food_name(d) or "Surplus Food"
        exp_dt = donation_expires_at(d)
        exp_str = format_expiry_strings(exp_dt)[2]

        if d.status in ("Active", "Available"):
            events.append(
                {
                    "id": f"don-{d.donation_id}-avail",
                    "type": "donation",
                    "title": (
                        "⚡ TTT Auto-Donation Available!"
                        if "ttt" in (d.trigger_type or "").lower()
                        else "New Surplus Donation Available!"
                    ),
                    "detail": f"{merchant_name} posted {d.qty_donated} {unit_str} of {food_str} (Exp: {exp_str}).",
                    "time": d.created_at,
                    "target_route": "/foodbank/donations",
                    "is_read": False,
                }
            )
        elif d.status in (
            "Claimed",
            "Ongoing",
            "In-Transit",
            "Completed",
            "Successful",
            "Expired",
        ):
            events.append(
                {
                    "id": f"don-{d.donation_id}-{d.status.lower()}",
                    "type": "donation",
                    "title": (
                        f"Donation Claimed — QR DON-{d.donation_id}"
                        if d.status in ("Claimed", "Ongoing")
                        else (
                            f"Donation Expired (DON-{d.donation_id})"
                            if d.status == "Expired"
                            else f"Donation Handshake Successful (DON-{d.donation_id})!"
                        )
                    ),
                    "detail": f"{d.qty_donated} {unit_str} of {food_str} from {merchant_name} ({d.status}).",
                    "time": d.updated_at or d.created_at,
                    "target_route": "/foodbank/donations",
                    "is_read": False,
                }
            )

    events.sort(key=lambda e: e["time"], reverse=True)
    for e in events:
        e["sort_ts"] = e["time"].timestamp()
        e["time"] = (
            timezone.localtime(e["time"]).strftime("%b %d, %Y • %I:%M %p")
            if timezone.is_aware(e["time"])
            else e["time"].isoformat()
        )
    return events[:limit]


class ActivityLogView(FoodBankAPIView):
    def get(self, request):
        auto_enforce_donation_safety_cutoffs()
        fb = resolve_foodbank(request)
        if not fb:
            return Response([], status=status.HTTP_200_OK)
        return Response(build_activity(fb=fb))


class FoodBankAnnouncementsView(FoodBankAPIView):
    """
    Returns system broadcasts from the Food Bank's registration date onward, sorted newest first.
    """

    def get(self, request):
        fb = resolve_foodbank(request)
        broadcasts_qs = AdminNotification.objects.filter(
            admin_notif_type__startswith="Broadcast"
        ).order_by("-pk")

        if fb and fb.user and fb.user.created_at:
            start_reg = fb.user.created_at.replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            broadcasts_qs = (
                broadcasts_qs.filter(created_at__gte=start_reg)
                | broadcasts_qs.filter(created_at__isnull=True)
            ).order_by("-pk")

        announcements = []
        for b in broadcasts_qs[:25]:
            audience = (
                b.admin_notif_type.replace("Broadcast:", "").strip()
                or "All Users (System-wide)"
            )
            if (
                "all" not in audience.lower()
                and "food" not in audience.lower()
                and "ngo" not in audience.lower()
            ):
                continue
            parts = (b.message or "").split(" || ", 1)
            title = parts[0] if len(parts) > 1 else "System Announcement"
            body = parts[1] if len(parts) > 1 else b.message
            b_dt = (
                timezone.localtime(b.created_at)
                if getattr(b, "created_at", None)
                else timezone.now()
            )
            announcements.append(
                {
                    "id": f"broadcast-{b.pk}",
                    "audience": audience,
                    "title": title,
                    "body": body,
                    "date": b_dt.strftime("%b %d, %Y • %I:%M %p"),
                    "sort_ts": b_dt.timestamp(),
                }
            )

        announcements.sort(key=lambda x: x.get("sort_ts", 0), reverse=True)
        return Response(announcements, status=status.HTTP_200_OK)


class DashboardView(FoodBankAPIView):
    def get(self, request):
        auto_enforce_donation_safety_cutoffs()
        fb = resolve_foodbank(request)
        if not fb:
            return Response(
                {"error": "Authenticated Food Bank account not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        fb_lat, fb_lng = get_foodbank_coords(fb, request)
        gov_radius = float(get_governance_config()["radius_km"])
        max_radius = float(request.GET.get("radius") or gov_radius)
        radius_label = format_radius_label(max_radius)

        my_requests = DonationRequest.objects.filter(food_bank=fb)
        active_requests = list(
            my_requests.filter(status__in=["Active", "Pending", "Ongoing"])
            .select_related("merchant")
            .order_by("-donation_req_id")
        )

        offers_qs = (
            Donation.objects.filter(status__in=["Active", "Available"])
            .select_related(*OFFER_SELECT)
            .prefetch_related(*OFFER_PREFETCH)
            .order_by("-donation_id")
        )
        offers = []
        for o in offers_qs:
            _, m_lat, m_lng = merchant_primary_location(o.merchant)
            if calculate_haversine_km(fb_lat, fb_lng, m_lat, m_lng) <= max_radius:
                offers.append(o)

        in_prog_count = Donation.objects.filter(
            food_bank=fb, status__in=["Claimed", "Ongoing", "In-Transit"]
        ).count()
        exp_soon = sum(
            1 for o in offers if (seconds_left(o) or 999999) < 10800
        )

        merchants_in_radius = 0
        for m in Merchant.objects.select_related("user").all():
            _, m_lat, m_lng = merchant_primary_location(m)
            if calculate_haversine_km(fb_lat, fb_lng, m_lat, m_lng) <= max_radius:
                merchants_in_radius += 1

        return Response(
            {
                "org_name": fb.org_name,
                "radius_km": radius_label,
                "stats": {
                    "radius_km": radius_label,
                    "total_requests": my_requests.count(),
                    "active_requests": len(active_requests),
                    "fulfilled_requests": my_requests.filter(
                        status__in=["Fulfilled", "Successful"]
                    ).count(),
                    "offers_available": len(offers),
                    "expiring_soon": exp_soon,
                    "in_progress": in_prog_count,
                    "food_received": float(fb.total_food_received or 0),
                    "partners": merchants_in_radius,
                },
                "urgent_offers": DonationSerializer(
                    offers[:5], many=True
                ).data,
                "active_requests": DonationRequestSerializer(
                    active_requests[:5], many=True
                ).data,
                "recent_activity": build_activity(fb=fb, limit=6),
            }
        )


class PartnersView(FoodBankAPIView):
    """
    Returns all verified Merchants within the specified radius of the Food Bank,
    including each Merchant's Active Listings (excluding items within 30 mins of expiry).
    """

    def get(self, request):
        auto_enforce_donation_safety_cutoffs()
        fb = resolve_foodbank(request)
        fb_lat, fb_lng = get_foodbank_coords(fb, request)
        gov_radius = float(get_governance_config()["radius_km"])
        max_radius = float(request.GET.get("radius") or gov_radius)
        radius_label = format_radius_label(max_radius)

        merchants = (
            Merchant.objects.select_related("user")
            .all()
            .order_by("-merch_id")
        )
        partners_list = []
        total_items_received = 0
        now = timezone.now()

        for m in merchants:
            m_addr, m_lat, m_lng = merchant_primary_location(m)
            dist_km = calculate_haversine_km(fb_lat, fb_lng, m_lat, m_lng)
            if dist_km > max_radius:
                continue

            m_phone, m_contact_name, m_email = merchant_contact_details(m)

            active_listings = []
            prods = (
                PerishableProduct.objects.filter(merchant=m)
                .select_related("category", "unit")
                .order_by("-prod_id")
            )
            for p in prods:
                saf = (
                    ListingSafety.objects.filter(product=p)
                    .select_related("commerce")
                    .first()
                )
                comm = getattr(saf, "commerce", None) if saf else None
                if not comm:
                    continue
                if (
                    comm.status not in ("Active",)
                    or int(comm.qty_available or 0) <= 0
                ):
                    continue

                exp_dt = saf.estimated_expiry if saf else None
                if exp_dt:
                    if timezone.is_naive(exp_dt):
                        exp_dt = timezone.make_aware(exp_dt)
                    if (exp_dt - now).total_seconds() <= 1800:
                        continue

                exp_date_s, exp_time_s, exp_disp = format_expiry_strings(
                    exp_dt
                )
                u_name = p.unit.unit_name if p.unit else "pcs"
                active_listings.append(
                    {
                        "prod_id": p.prod_id,
                        "commerce_id": comm.pk,
                        "name": p.prod_name,
                        "description": p.prod_desc
                        or "Fresh surplus food available for community rescue.",
                        "category": (
                            p.category.category_name
                            if p.category
                            else (m.merch_type or "Surplus")
                        ),
                        "qty_available": int(comm.qty_available or 0),
                        "unit_name": u_name,
                        "orig_price": f"₱{float(comm.orig_price or 0):,.2f}",
                        "disc_price": f"₱{float(comm.discounted_price or 0):,.2f}",
                        "expiry_date": exp_date_s,
                        "expiry_time": exp_time_s,
                        "expiry_display": exp_disp,
                        "expires_at": exp_dt.isoformat() if exp_dt else None,
                        "image": p.image_path
                        or "https://images.unsplash.com/photo-1509440159596-0249088772ff?auto=format&fit=crop&w=600&q=80",
                    }
                )

            m_donations = Donation.objects.filter(merchant=m).order_by(
                "-created_at"
            )
            active_offers = m_donations.filter(
                status__in=["Active", "Available"]
            ).count()
            completed_dons = m_donations.filter(
                status__in=["Completed", "Successful", "Claimed", "Ongoing"]
            )
            rec_qty = sum(int(d.qty_donated or 0) for d in completed_dons)
            req_filled = DonationRequest.objects.filter(
                merchant=m, status__in=["Fulfilled", "Successful"]
            ).count()
            total_items_received += rec_qty

            history = []
            for d in m_donations[:5]:
                history.append(
                    {
                        "food_name": donation_food_name(d) or "Surplus Bundle",
                        "qty": d.qty_donated,
                        "unit": donation_unit(d) or "units",
                        "status": d.status,
                        "date": (
                            timezone.localtime(d.created_at).strftime(
                                "%b %d, %Y"
                            )
                            if d.created_at
                            else ""
                        ),
                    }
                )

            last_dt = (
                m_donations.first().created_at.isoformat()
                if m_donations.first() and m_donations.first().created_at
                else (m.user.created_at.isoformat() if m.user else "")
            )
            partners_list.append(
                {
                    "merchant_id": m.merch_id,
                    "name": m.bus_name,
                    "type": m.merch_type or "Food Establishment",
                    "is_verified": m.is_verified,
                    "address": m_addr,
                    "latitude": m_lat,
                    "longitude": m_lng,
                    "distance_km": dist_km,
                    "distance": f"{dist_km:.1f} km",
                    "contact_phone": m_phone,
                    "hotline": m.hotline or "N/A",
                    "contact_name": m_contact_name,
                    "contact_email": m_email,
                    "donations_count": m_donations.count(),
                    "received_qty": rec_qty,
                    "requests_fulfilled": req_filled,
                    "active_offers": active_offers,
                    "active_listings_count": len(active_listings),
                    "active_listings": active_listings,
                    "last_activity": last_dt,
                    "history": history,
                }
            )

        partners_list.sort(key=lambda x: x.get("distance_km", 99.0))
        return Response(
            {
                "radius_km": radius_label,
                "partners": partners_list,
                "summary": {
                    "radius_km": radius_label,
                    "count": len(partners_list),
                    "items_received": total_items_received,
                },
            }
        )


class FoodBankProfilePortalView(FoodBankAPIView):
    def get(self, request, org_id):
        fb = resolve_foodbank(request)
        if org_id and str(org_id).isdigit() and int(org_id) > 0:
            found = (
                FoodBank.objects.select_related("user")
                .filter(org_id=int(org_id))
                .first()
            )
            if found:
                fb = found
        if not fb:
            return Response(
                {"error": "Food Bank not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        loc = (
            Location.objects.filter(user=fb.user)
            .order_by("-is_primary", "-loc_id")
            .first()
        )
        return Response(
            {
                "org_id": fb.org_id,
                "org_name": fb.org_name,
                "contact_person": fb.contact_person or "Coordinator",
                "email": fb.user.user_email,
                "phone": fb.phone or "",
                "hotline": fb.hotline or "",
                "address": fb.address or "Cebu City",
                "status": fb.status,
                "accreditation_path": fb.accreditation_path or "",
                "total_food_received": float(fb.total_food_received or 0),
                "latitude": float(loc.latitude) if loc else 10.3157,
                "longitude": float(loc.longitude) if loc else 123.8854,
            }
        )

    def post(self, request, org_id):
        fb = resolve_foodbank(request)
        if org_id and str(org_id).isdigit() and int(org_id) > 0:
            found = (
                FoodBank.objects.select_related("user")
                .filter(org_id=int(org_id))
                .first()
            )
            if found:
                fb = found
        if not fb:
            return Response(
                {"error": "Food Bank not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        action = request.data.get("action")
        if action == "update_profile":
            if (
                "org_name" in request.data
                and str(request.data["org_name"]).strip()
            ):
                fb.org_name = str(request.data["org_name"]).strip()
            if (
                "contact_person" in request.data
                and str(request.data["contact_person"]).strip()
            ):
                fb.contact_person = str(request.data["contact_person"]).strip()
            if "phone" in request.data and str(request.data["phone"]).strip():
                fb.phone = str(request.data["phone"]).strip()
            if "hotline" in request.data:
                fb.hotline = str(request.data["hotline"]).strip()
            if (
                "address" in request.data
                and str(request.data["address"]).strip()
            ):
                fb.address = str(request.data["address"]).strip()
            fb.save()

            target_email = (
                str(
                    request.data.get("new_email")
                    or request.data.get("email")
                    or ""
                )
                .strip()
                .lower()
            )
            if target_email and target_email != fb.user.user_email.lower():
                if (
                    User.objects.exclude(user_id=fb.user.user_id)
                    .filter(user_email__iexact=target_email)
                    .exists()
                ):
                    return Response(
                        {
                            "error": "That email address is already registered to another account."
                        },
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                fb.user.user_email = target_email
                fb.user.save()

            if "latitude" in request.data and "longitude" in request.data:
                try:
                    lat_v = float(request.data["latitude"])
                    lng_v = float(request.data["longitude"])
                    loc_obj = (
                        Location.objects.filter(user=fb.user)
                        .order_by("-is_primary", "-loc_id")
                        .first()
                    )
                    if loc_obj:
                        loc_obj.address_name = fb.address or "Cebu City"
                        loc_obj.latitude = lat_v
                        loc_obj.longitude = lng_v
                        loc_obj.save()
                    else:
                        Location.objects.create(
                            user=fb.user,
                            address_name=fb.address or "Cebu City",
                            latitude=lat_v,
                            longitude=lng_v,
                            is_primary=True,
                        )
                except Exception:
                    pass
            return Response(
                {
                    "message": "Food Bank profile and Cebu City location updated!",
                    "org_name": fb.org_name,
                    "email": fb.user.user_email,
                }
            )

        elif action == "change_password":
            current_pw = str(request.data.get("current_password") or "").strip()
            new_pw = str(request.data.get("new_password") or "").strip()
            if len(new_pw) < 6:
                return Response(
                    {"error": "Password must be at least 6 characters."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            if current_pw and fb.user.user_pass:
                is_valid = check_password(current_pw, fb.user.user_pass) or (
                    current_pw == fb.user.user_pass
                )
                if not is_valid:
                    return Response(
                        {"error": "Your current password is incorrect."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )

            fb.user.user_pass = make_password(new_pw)
            fb.user.save()
            return Response({"message": "Password updated successfully!"})

        elif action == "deactivate_account":
            fb.status = "Inactive"
            fb.status_reason = (
                "Organization account deactivated/deleted by user"
            )
            fb.save()
            fb.user.is_banned = True
            fb.user.save()

            notify_party(
                None,
                f"Food Bank FB-{fb.org_id} ({fb.org_name}) deactivated their account (Status set to Inactive).",
                notif_type="Compliance",
            )
            return Response(
                {
                    "message": "Your organization account has been set to Inactive."
                }
            )

        return Response(
            {"error": "Invalid action"}, status=status.HTTP_400_BAD_REQUEST
        )