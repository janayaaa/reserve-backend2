import json
import re
import random
import uuid
import requests as http_requests
from datetime import datetime, timedelta
from decimal import Decimal
from django.db import transaction
from django.utils import timezone
from django.conf import settings
from django.db.models import Avg
from django.contrib.auth.hashers import make_password, check_password
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import AllowAny

from .utils import get_cebu_weather, calculate_ttt_expiry, calculate_haversine_km
from .models import (
    PerishableProduct,
    ListingSafety,
    ListingCommerce,
    Category,
    UnitMetrics,
    SafetyThreshold,
    StockLedger,
)
from accounts.models import Merchant, Consumer, User, SubscriptionPlan, Location
from donations.models import Donation
from notifications.models import AdminNotification, UserNotification
from transactions.models import (
    OrderReservation,
    PaymentTransaction,
    WithdrawalReq,
    RatingReview,
)
from .serializers import PerishableProductSerializer
from accounts.governance import get_governance_config


def _get_normalized_gov():
    gov = dict(get_governance_config() or {})
    if float(gov.get("premium_monthly_price", 149.0)) == 399.0:
        gov["premium_monthly_price"] = 149.0
    gov.setdefault("premium_monthly_price", 149.0)
    gov.setdefault("commission_pct", 10.0)
    gov.setdefault("pro_commission_pct", 4.0)
    gov.setdefault("deposit_pct", 20.0)
    gov.setdefault("min_withdrawal", 100.0)
    return gov


def resolve_order_live_pricing(order_obj, tx_map=None):
    """
    Returns (unit_price, orig_unit_price, line_total, orig_line_total, deposit_paid, remaining_balance)
    reflecting the exact live dynamic price locked in when the consumer checked out.
    """
    qty = max(1, int(order_obj.order_qty or 1))
    commerce = order_obj.listing
    fallback_disc = Decimal(
        str(
            (commerce.current_price or commerce.discounted_price)
            if commerce
            else 0
        )
    ).quantize(Decimal("0.01"))
    orig_unit = Decimal(
        str(
            commerce.orig_price
            if (commerce and commerce.orig_price)
            else fallback_disc
        )
    ).quantize(Decimal("0.01"))
    orig_line_total = (orig_unit * Decimal(qty)).quantize(Decimal("0.01"))

    dep_raw = Decimal(str(order_obj.downpayment_amount or 0)).quantize(
        Decimal("0.01")
    )

    tx = None
    if tx_map is not None:
        tx = tx_map.get(order_obj.order_id)
    else:
        tx = PaymentTransaction.objects.filter(
            order_id=order_obj.order_id
        ).first()

    tx_gross = Decimal("0.00")
    if tx:
        tx_gross = (
            Decimal(str(tx.commission_taken or 0))
            + Decimal(str(tx.merchant_earnings or 0))
        ).quantize(Decimal("0.01"))

    if tx_gross > dep_raw and tx_gross > Decimal("0.00"):
        line_total = tx_gross
        deposit_paid = (
            dep_raw
            if dep_raw > Decimal("0.00")
            else (line_total * Decimal("0.20")).quantize(Decimal("0.01"))
        )
    elif dep_raw > Decimal("0.00"):
        line_total = (dep_raw * Decimal("5.00")).quantize(Decimal("0.01"))
        deposit_paid = dep_raw
    else:
        line_total = (fallback_disc * Decimal(qty)).quantize(Decimal("0.01"))
        deposit_paid = (line_total * Decimal("0.20")).quantize(Decimal("0.01"))

    unit_price = (line_total / Decimal(qty)).quantize(Decimal("0.01"))
    remaining_balance = max(
        Decimal("0.00"), (line_total - deposit_paid).quantize(Decimal("0.01"))
    )
    return (
        unit_price,
        orig_unit,
        line_total,
        orig_line_total,
        deposit_paid,
        remaining_balance,
    )


def ensure_listing_reference_data():
    gov = _get_normalized_gov()
    SubscriptionPlan.objects.update_or_create(
        plan_id=1,
        defaults={
            "plan_name": "Basic Tier (Free)",
            "commission_rate": Decimal(str(gov["commission_pct"])),
        },
    )
    SubscriptionPlan.objects.update_or_create(
        plan_id=2,
        defaults={
            "plan_name": "Pro Tier (₱149/mo)",
            "commission_rate": Decimal(str(gov.get("pro_commission_pct", 4.0))),
        },
    )

    for uid, uname in {
        1: "Gram (g)",
        2: "Kilogram (kg)",
        3: "Piece (pc)",
        4: "Pack (pk)",
    }.items():
        UnitMetrics.objects.get_or_create(unit_id=uid, defaults={"unit_name": uname})

    categories = {
        1: "Plain",
        2: "Sweet",
        3: "Egg-Based",
        4: "Custom/Others",
        5: "Leafy Greens",
        6: "Vegetables",
        7: "Fruits",
        8: "Root Crops",
    }
    for cid, cname in categories.items():
        Category.objects.update_or_create(
            category_id=cid, defaults={"category_name": cname}
        )

    SafetyThreshold.objects.get_or_create(
        threshold_id=1,
        defaults={
            "food_category": "General Perishable",
            "storage_type": "Standard",
            "max_safe_hours": 48,
        },
    )


def resolve_merchant(raw_id=None, email=None):
    if raw_id and str(raw_id).isdigit() and int(raw_id) > 0:
        m = (
            Merchant.objects.select_related("user", "plan")
            .filter(merch_id=int(raw_id))
            .first()
        )
        if m:
            return m
    if email:
        m = (
            Merchant.objects.select_related("user", "plan")
            .filter(user__user_email__iexact=str(email).strip())
            .first()
        )
        if m:
            return m
    return None


def get_merchant_shop_hours(merchant):
    """
    Reads shop_open_time / shopOpenTime, shop_close_time / shopCloseTime, and
    shop_hours_enabled / shopHoursEnabled from merchant.user.settings_json.
    """
    open_str = "08:00 AM"
    close_str = "08:00 PM"
    hours_enabled = True
    if merchant and merchant.user and merchant.user.settings_json:
        try:
            cfg = json.loads(merchant.user.settings_json or "{}")
            if isinstance(cfg, dict):
                raw_open = cfg.get("shopOpenTime") or cfg.get("shop_open_time")
                raw_close = cfg.get("shopCloseTime") or cfg.get("shop_close_time")
                if raw_open:
                    open_str = str(raw_open).strip()
                if raw_close:
                    close_str = str(raw_close).strip()
                if "shopHoursEnabled" in cfg:
                    hours_enabled = bool(cfg["shopHoursEnabled"])
                elif "shop_hours_enabled" in cfg:
                    hours_enabled = bool(cfg["shop_hours_enabled"])
        except Exception:
            pass
    return open_str, close_str, hours_enabled


def parse_time_str_to_minutes(t_str):
    if not t_str:
        return None
    clean = re.sub(r"\s+", " ", str(t_str).replace("\u202f", " ").strip()).upper()
    for fmt in ("%I:%M %p", "%H:%M", "%I %p"):
        try:
            dt = datetime.strptime(clean, fmt)
            return dt.hour * 60 + dt.minute
        except ValueError:
            continue
    return None


def is_merchant_currently_open(merchant):
    open_str, close_str, hours_enabled = get_merchant_shop_hours(merchant)
    if not hours_enabled:
        return True, open_str, close_str

    open_mins = parse_time_str_to_minutes(open_str)
    close_mins = parse_time_str_to_minutes(close_str)
    if open_mins is None or close_mins is None:
        return True, open_str, close_str

    now_local = timezone.localtime(timezone.now())
    curr_mins = now_local.hour * 60 + now_local.minute

    if open_mins <= close_mins:
        is_open = open_mins <= curr_mins <= close_mins
    else:
        is_open = curr_mins >= open_mins or curr_mins <= close_mins

    return is_open, open_str, close_str


def parse_frontend_expiry(expiry_date_str, expiry_time_str):
    if not expiry_date_str or not expiry_time_str:
        return None
    try:
        clean_date = (
            str(expiry_date_str).replace(",", "").replace("Sept ", "Sep ").strip()
        )
        clean_time = re.sub(
            r"\s+",
            " ",
            str(expiry_time_str).replace("\u202f", " ").replace("\xa0", " "),
        ).strip()
        combined_str = f"{clean_date} {clean_time}"
        naive_dt = datetime.strptime(combined_str, "%b %d %Y %I:%M %p")
        return timezone.make_aware(naive_dt)
    except Exception as e:
        print("Expiry parse fallback notice:", e)
        return None


def parse_frontend_prep_datetime(prep_date_str):
    if not prep_date_str:
        return timezone.now()
    try:
        cleaned_str = (
            str(prep_date_str)
            .replace(",", "")
            .replace("Sept ", "Sep ")
            .replace("\u202f", " ")
            .replace("\xa0", " ")
            .strip()
        )
        cleaned_str = re.sub(r"\s+", " ", cleaned_str)
        try:
            prep_dt = datetime.strptime(cleaned_str, "%b %d %Y %I:%M %p")
        except ValueError:
            prep_dt = datetime.strptime(cleaned_str, "%b %d %Y")

        if timezone.is_naive(prep_dt):
            prep_dt = timezone.make_aware(prep_dt)
        return prep_dt
    except Exception as e:
        print("Prep date parse fallback notice:", e)
        return timezone.now()


def compute_base_shelf_hours(
    category_name, secondary_class="Plain", storage="Ambient"
):
    """
    Determines baseline shelf-life hours before applying the Arrhenius TTT
    live Cebu City temperature adjustment.
    """
    is_chilled = "Chilled" in str(storage or "")
    is_frozen = "Frozen" in str(storage or "")

    cat_clean = str(category_name or "").strip()
    sec_clean = str(secondary_class or "").strip()

    produce_hours = {
        "Leafy Greens": 48.0,
        "Vegetables": 72.0,
        "Fruits": 96.0,
        "Root Crops": 240.0,
    }
    if cat_clean in produce_hours:
        return produce_hours[cat_clean]

    if sec_clean == "Egg-Based" or cat_clean in ("Cake", "Egg-Based"):
        return 72.0 if is_chilled else 24.0
    if sec_clean == "Sweet" or cat_clean in ("Pastry", "Pastries", "Sweet"):
        return 120.0 if is_chilled else 48.0
    if sec_clean == "Plain" or cat_clean in ("Bread", "Plain"):
        return 168.0 if is_chilled else 72.0

    base = 48.0
    if is_frozen:
        return base * 5.0
    if is_chilled:
        return base * 2.5
    return base


def base_voucher_code(raw_code):
    return str(raw_code or "").strip().upper().split("-")[0]


def verify_xendit_invoice_paid(invoice_id):
    """
    Queries the Xendit v2 Invoices API to verify whether an invoice is PAID or SETTLED.
    """
    clean_id = str(invoice_id or "").strip()
    if not clean_id:
        return False, "MISSING", {}, "Missing Xendit invoice ID."

    xendit_key = getattr(settings, "XENDIT_SECRET_KEY", "").strip()
    if not xendit_key or not xendit_key.startswith("xnd_"):
        return (
            False,
            "CONFIG_ERROR",
            {},
            "XENDIT_SECRET_KEY is missing or invalid in backend .env.",
        )

    try:
        res = http_requests.get(
            f"https://api.xendit.co/v2/invoices/{clean_id}",
            auth=(xendit_key, ""),
            timeout=10,
        )
        data = res.json()
        if res.status_code == 200 and isinstance(data, dict):
            inv_status = str(data.get("status", "PENDING")).upper()
            is_paid = inv_status in ("PAID", "SETTLED")
            return is_paid, inv_status, data, ""
        return (
            False,
            "ERROR",
            data if isinstance(data, dict) else {},
            data.get("message", "Could not retrieve Xendit invoice status."),
        )
    except Exception as e:
        return False, "ERROR", {}, str(e)


def disburse_xendit_payout(
    channel, account_number, account_name, net_amount_dec, external_id
):
    """
    Executes an automated E-Wallet payout from Xendit to the merchant's GCash or Maya account
    using Xendit's v2 Payouts API (https://api.xendit.co/v2/payouts) with v1 /disbursements fallback.
    """
    xendit_key = getattr(settings, "XENDIT_SECRET_KEY", "").strip()
    is_gcash = "MAYA" not in str(channel or "GCash").upper()
    v2_channel_code = "PH_GCASH" if is_gcash else "PH_PAYMAYA"
    v1_bank_code = "GCASH" if is_gcash else "PAYMAYA"

    clean_acct = re.sub(r"\D", "", str(account_number or "")).strip()
    if clean_acct.startswith("639") and len(clean_acct) == 12:
        clean_acct = "0" + clean_acct[2:]
    elif clean_acct.startswith("9") and len(clean_acct) == 10:
        clean_acct = "0" + clean_acct

    fallback_ref = f"XND-{str(channel).upper()}-{uuid.uuid4().hex[:8].upper()}"

    if not xendit_key or not xendit_key.startswith("xnd_"):
        return fallback_ref

    amount_num = float(Decimal(str(net_amount_dec)).quantize(Decimal("0.01")))

    try:
        v2_res = http_requests.post(
            "https://api.xendit.co/v2/payouts",
            headers={
                "Idempotency-Key": external_id,
                "Content-Type": "application/json",
            },
            json={
                "reference_id": external_id,
                "channel_code": v2_channel_code,
                "channel_properties": {
                    "account_holder_name": account_name or "ReServe Merchant",
                    "account_number": clean_acct,
                },
                "amount": amount_num,
                "description": f"RESERVE Merchant Wallet Payout ({channel})",
                "currency": "PHP",
            },
            auth=(xendit_key, ""),
            timeout=12,
        )
        v2_data = v2_res.json() if v2_res.content else {}
        if (
            v2_res.status_code in (200, 201)
            and isinstance(v2_data, dict)
            and v2_data.get("id")
        ):
            return str(v2_data["id"])
        print("Xendit v2 payout response:", v2_res.status_code, v2_data)
    except Exception as e:
        print("Xendit v2 payout notice:", e)

    try:
        v1_res = http_requests.post(
            "https://api.xendit.co/disbursements",
            json={
                "external_id": external_id,
                "bank_code": v1_bank_code,
                "account_holder_name": account_name or "ReServe Merchant",
                "account_number": clean_acct,
                "description": f"RESERVE Merchant Wallet Payout ({channel})",
                "amount": max(1, int(round(amount_num))),
            },
            auth=(xendit_key, ""),
            timeout=10,
        )
        v1_data = v1_res.json() if v1_res.content else {}
        if (
            v1_res.status_code in (200, 201)
            and isinstance(v1_data, dict)
            and v1_data.get("id")
        ):
            return f"XND-{str(v1_data['id'])[-10:].upper()}"
        print("Xendit v1 disbursement response:", v1_res.status_code, v1_data)
    except Exception as e:
        print("Xendit v1 disbursement notice:", e)

    return fallback_ref


def auto_flag_expiring_listings(merchant=None):
    """
    Runs the unified TTT Auto-Donate (60m-31m) and 30-Minute Safety Cutoff enforcer.
    """
    try:
        from donations.views import auto_enforce_donation_safety_cutoffs

        auto_enforce_donation_safety_cutoffs()
    except Exception as e:
        print("TTT safety enforcer notice:", e)


def get_rolling_month_labels(count=6):
    today = timezone.now().date().replace(day=1)
    months = []
    curr = today
    for _ in range(count):
        months.append(curr.strftime("%b"))
        curr = (curr - timedelta(days=1)).replace(day=1)
    months.reverse()
    return months


class MarketplaceFeedView(APIView):
    """
    Strictly returns ONLY Active, non-expired, non-flagged listings with available stock > 0
    within the consumer's Haversine discovery radius, enriched with merchant operating hours.
    """

    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        ensure_listing_reference_data()
        auto_flag_expiring_listings()

        donated_commerce_ids = Donation.objects.values_list("listing_id", flat=True)
        donated_product_ids = ListingCommerce.objects.filter(
            listing_id__in=donated_commerce_ids
        ).values_list("listing__product_id", flat=True)

        cons_lat, cons_lng = 10.3157, 123.8854
        req_lat = request.GET.get("lat")
        req_lng = request.GET.get("lng")
        cons_id = request.GET.get("consumer")
        email = request.GET.get("email")

        if req_lat and req_lng:
            try:
                cons_lat, cons_lng = float(req_lat), float(req_lng)
            except ValueError:
                pass
        else:
            consumer_obj = None
            if cons_id and str(cons_id).isdigit() and int(cons_id) > 0:
                consumer_obj = (
                    Consumer.objects.select_related("user")
                    .filter(cons_id=int(cons_id))
                    .first()
                )
            if not consumer_obj and email:
                consumer_obj = (
                    Consumer.objects.select_related("user")
                    .filter(user__user_email__iexact=str(email).strip())
                    .first()
                )
            if consumer_obj:
                loc = (
                    Location.objects.filter(user=consumer_obj.user)
                    .order_by("-is_primary", "-loc_id")
                    .first()
                )
                if loc:
                    cons_lat, cons_lng = float(loc.latitude), float(loc.longitude)

        now = timezone.now()
        listings = list(
            PerishableProduct.objects.select_related(
                "category", "unit", "merchant__user"
            ).exclude(prod_id__in=donated_product_ids)
        )

        prod_ids = [item.prod_id for item in listings]
        safety_map = {}
        for s in ListingSafety.objects.filter(product_id__in=prod_ids).select_related(
            "commerce"
        ):
            if s.product_id not in safety_map:
                safety_map[s.product_id] = s

        merch_user_ids = {
            item.merchant.user_id
            for item in listings
            if item.merchant and item.merchant.user_id
        }
        loc_map = {}
        for l in Location.objects.filter(user_id__in=merch_user_ids).order_by(
            "-is_primary", "-loc_id"
        ):
            if l.user_id not in loc_map:
                loc_map[l.user_id] = (float(l.latitude), float(l.longitude))

        gov = _get_normalized_gov()
        max_radius_km = float(request.GET.get("radius") or gov["radius_km"])
        serialized_feed = []

        for item in listings:
            safety_rec = safety_map.get(item.prod_id)
            if not safety_rec:
                continue

            comm = getattr(safety_rec, "commerce", None)
            if not comm:
                continue

            if str(comm.status or "").strip() != "Active":
                continue
            if int(comm.qty_available or 0) <= 0:
                continue

            if safety_rec.estimated_expiry:
                expiry_dt = safety_rec.estimated_expiry
                if timezone.is_naive(expiry_dt):
                    expiry_dt = timezone.make_aware(expiry_dt)

                if (expiry_dt - now).total_seconds() <= 1830:
                    if comm.status == "Active":
                        comm.status = "Flagged"
                        comm.save(update_fields=["status"])
                    continue

            merch_lat, merch_lng = 10.3157, 123.8854
            if item.merchant and item.merchant.user_id in loc_map:
                merch_lat, merch_lng = loc_map[item.merchant.user_id]

            dist_km = calculate_haversine_km(cons_lat, cons_lng, merch_lat, merch_lng)

            if dist_km <= max_radius_km:
                open_str, close_str, hours_enabled = get_merchant_shop_hours(
                    item.merchant
                )
                is_open, _, _ = is_merchant_currently_open(item.merchant)
                item_data = PerishableProductSerializer(item).data
                if str(item_data.get("status", "Active")).strip() != "Active":
                    continue
                item_data["distance_km"] = dist_km
                item_data["distance"] = f"{dist_km:.1f} km"
                item_data["radius_km"] = gov["radius_km"]
                item_data["deposit_pct"] = gov.get("deposit_pct", 20.0)
                item_data["merchant_lat"] = merch_lat
                item_data["merchant_lng"] = merch_lng
                item_data["is_shop_open"] = is_open
                item_data["shop_open_time"] = open_str
                item_data["shop_close_time"] = close_str
                item_data["shop_hours_enabled"] = hours_enabled
                item_data["posted_temp"] = (
                    float(safety_rec.posted_temp)
                    if safety_rec.posted_temp is not None
                    else 30.0
                )
                item_data["humidity_index"] = (
                    float(safety_rec.humidity_index)
                    if safety_rec.humidity_index is not None
                    else 75.0
                )
                serialized_feed.append(item_data)

        serialized_feed.sort(key=lambda x: x.get("distance_km", 99.0))
        return Response(serialized_feed, status=status.HTTP_200_OK)


class EstablishmentListingsView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        ensure_listing_reference_data()

        merchant = resolve_merchant(
            request.GET.get("merchant"), request.GET.get("email")
        )
        if not merchant:
            return Response(
                {"listings": [], "flagged_count": 0}, status=status.HTTP_200_OK
            )

        auto_flag_expiring_listings(merchant=merchant)

        listings = PerishableProduct.objects.filter(merchant=merchant).select_related(
            "category", "merchant"
        )

        donated_commerce_ids = Donation.objects.values_list("listing_id", flat=True)
        donated_product_ids = ListingCommerce.objects.filter(
            listing_id__in=donated_commerce_ids
        ).values_list("listing__product_id", flat=True)

        listings = listings.exclude(prod_id__in=donated_product_ids)

        prod_ids = [item.prod_id for item in listings]
        safety_map = {
            s.product_id: s
            for s in ListingSafety.objects.filter(product_id__in=prod_ids)
        }

        serialized_data = []
        flagged_count = 0

        for item in listings:
            serializer = PerishableProductSerializer(item)
            data = dict(serializer.data)
            s_rec = safety_map.get(item.prod_id)
            if s_rec:
                p_temp = (
                    float(s_rec.posted_temp)
                    if s_rec.posted_temp is not None
                    else 30.0
                )
                h_idx = (
                    float(s_rec.humidity_index)
                    if s_rec.humidity_index is not None
                    else 75.0
                )
                data["posted_temp"] = p_temp
                data["humidity_index"] = h_idx
                if "market" in str(merchant.merch_type or "").lower():
                    data["storage_condition"] = f"Ambient ({p_temp:.1f}°C)"
                elif p_temp == 4.0:
                    data["storage_condition"] = "Chilled (4°C)"
                else:
                    data["storage_condition"] = f"Ambient ({p_temp:.1f}°C)"

            item_status = data.get("status", "Active")
            if item_status == "Flagged":
                flagged_count += 1
            serialized_data.append(data)

        return Response(
            {"listings": serialized_data, "flagged_count": flagged_count},
            status=status.HTTP_200_OK,
        )


class CalculateExpiryPreviewView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        prep_date_str = request.GET.get("prep_date", None)
        storage = request.GET.get("storage", "Ambient")
        category_name = request.GET.get("category", "Plain")
        secondary_class = request.GET.get("secondary_classification", "Plain")

        current_temp, current_humidity = get_cebu_weather()
        prep_dt = parse_frontend_prep_datetime(prep_date_str)

        base_hours = compute_base_shelf_hours(
            category_name, secondary_class, storage
        )

        if "Chilled" in storage or "Frozen" in storage:
            calculated_expiry = prep_dt + timedelta(hours=base_hours)
            effective_temp = 4.0 if "Chilled" in storage else -18.0
        else:
            calculated_expiry = calculate_ttt_expiry(
                base_hours, current_temp, prep_dt
            )
            effective_temp = float(current_temp)

        local_expiry = timezone.localtime(calculated_expiry)

        return Response(
            {
                "date": local_expiry.strftime("%b %d, %Y"),
                "time": local_expiry.strftime("%I:%M %p").lstrip("0"),
                "temp": round(float(current_temp), 1),
                "humidity": int(current_humidity),
                "effective_temp": round(float(effective_temp), 1),
                "base_hours": base_hours,
            },
            status=status.HTTP_200_OK,
        )


class CreateListingView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        ensure_listing_reference_data()

        mutable_data = (
            request.data.copy()
            if hasattr(request.data, "copy")
            else dict(request.data)
        )

        merchant = resolve_merchant(
            mutable_data.get("merchant"), mutable_data.get("email")
        )
        if not merchant:
            return Response(
                {"error": "Authenticated merchant account not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        mutable_data["merchant"] = merchant.merch_id

        gov = _get_normalized_gov()
        free_cap = int(gov["free_cap"])
        if merchant.plan_id == 1:
            weekly_count = PerishableProduct.objects.filter(merchant=merchant).count()
            if weekly_count >= free_cap:
                return Response(
                    {
                        "error": f"Basic Tier Limit Reached: You have reached the maximum of {free_cap} listings per week. Upgrade to Pro Tier (₱{gov['premium_monthly_price']:,.2f}/mo) in your Profile for unlimited listings and 4% low commission!"
                    },
                    status=status.HTTP_403_FORBIDDEN,
                )

        sec_class = mutable_data.get("secondary_classification", "Plain")
        price_mode = mutable_data.get("pricing_mode", "dynamic")
        mutable_data["prod_type"] = f"{sec_class}|{price_mode}"

        if not mutable_data.get("threshold"):
            mutable_data["threshold"] = 1

        serializer = PerishableProductSerializer(
            data=mutable_data, context={"request": request}
        )

        if serializer.is_valid():
            new_listing = serializer.save()
            current_temp, current_humidity = get_cebu_weather()

            storage = str(mutable_data.get("storage_condition", "Ambient"))
            is_market_vendor = "market" in str(merchant.merch_type or "").lower()

            if is_market_vendor:
                posted_temp = float(current_temp)
                storage = f"Ambient ({current_temp:.1f}°C)"
            elif "Frozen" in storage:
                posted_temp = -18.00
            elif "Chilled" in storage:
                posted_temp = 4.00
            else:
                posted_temp = float(current_temp)

            cat_obj = new_listing.category
            cat_name = cat_obj.category_name if cat_obj else sec_class
            base_hours = compute_base_shelf_hours(cat_name, sec_class, storage)
            prep_dt = parse_frontend_prep_datetime(
                mutable_data.get("preparation_date")
            )

            final_expiry = parse_frontend_expiry(
                mutable_data.get("expiry_date"), mutable_data.get("expiry_time")
            )
            if not final_expiry:
                if "Chilled" in storage or "Frozen" in storage:
                    final_expiry = prep_dt + timedelta(hours=base_hours)
                else:
                    final_expiry = calculate_ttt_expiry(
                        base_hours, current_temp, prep_dt
                    )

            safety_record = ListingSafety.objects.create(
                product=new_listing,
                safely_anchor_time=mutable_data.get("preparation_date"),
                anchor_type="Preparation",
                estimated_expiry=final_expiry,
                posted_temp=posted_temp,
                humidity_index=current_humidity,
            )

            raw_qty = mutable_data.get("quantity") or mutable_data.get(
                "qty_available", 0
            )
            try:
                parsed_qty = int(float(raw_qty)) if raw_qty not in ("", None) else 0
            except (ValueError, TypeError):
                parsed_qty = 0

            listing_status = mutable_data.get("status", "Active")
            if (final_expiry - timezone.now()).total_seconds() <= 1830:
                listing_status = "Flagged"

            ListingCommerce.objects.create(
                listing=safety_record,
                orig_price=mutable_data.get("orig_price")
                or mutable_data.get("original_price")
                or 0.00,
                discounted_price=mutable_data.get("discounted_price")
                or mutable_data.get("price")
                or 0.00,
                floor_price=mutable_data.get("floor_price") or 0.00,
                current_price=mutable_data.get("discounted_price")
                or mutable_data.get("price")
                or 0.00,
                qty_available=parsed_qty,
                pickup_deadline=final_expiry,
                status=listing_status,
            )

            exp_fmt = timezone.localtime(final_expiry).strftime("%b %d, %Y %I:%M %p")
            if merchant and merchant.user:
                UserNotification.objects.create(
                    user=merchant.user,
                    message=f"Listing '{new_listing.prod_name}' published ({parsed_qty} available). Storage temp: {posted_temp:.1f}°C ({current_humidity}% RH) • Expiry: {exp_fmt}.",
                )
            AdminNotification.objects.create(
                user_id=1,
                admin_notif_type="Listing",
                message=f"{merchant.bus_name if merchant else 'Merchant'} posted '{new_listing.prod_name}' (Exp: {exp_fmt} @ {posted_temp:.1f}°C).",
                merchant=merchant,
            )

            return Response(
                {
                    "message": "Listing, Safety, and Pricing created successfully!",
                    "data": PerishableProductSerializer(new_listing).data,
                },
                status=status.HTTP_201_CREATED,
            )

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class UpdateListingView(APIView):
    permission_classes = [AllowAny]

    def put(self, request, pk):
        ensure_listing_reference_data()

        try:
            listing = PerishableProduct.objects.select_related("merchant__user").get(
                pk=pk
            )
        except PerishableProduct.DoesNotExist:
            return Response(
                {"error": "Listing not found"}, status=status.HTTP_404_NOT_FOUND
            )

        listing.prod_name = request.data.get("prod_name", listing.prod_name)
        listing.prod_desc = request.data.get("prod_desc", listing.prod_desc)
        listing.weight_per_unit = request.data.get(
            "weight_per_unit", listing.weight_per_unit or 0
        )
        if request.data.get("image_path"):
            listing.image_path = request.data.get("image_path")

        if (
            "secondary_classification" in request.data
            or "pricing_mode" in request.data
        ):
            raw_existing = (listing.prod_type or "Plain|dynamic").split("|")
            sec_class = request.data.get(
                "secondary_classification", raw_existing[0]
            )
            price_mode = request.data.get(
                "pricing_mode",
                raw_existing[1] if len(raw_existing) > 1 else "dynamic",
            )
            listing.prod_type = f"{sec_class}|{price_mode}"

        unit_id = request.data.get("unit") or request.data.get("unit_id")
        if unit_id:
            listing.unit_id = unit_id

        category_id = request.data.get("category")
        if category_id and str(category_id).isdigit():
            listing.category_id = int(category_id)

        listing.save()

        safety_record = ListingSafety.objects.filter(product=listing).first()
        if safety_record:
            current_temp, current_humidity = get_cebu_weather()
            safety_record.humidity_index = current_humidity

            prep_date_str = request.data.get("preparation_date")
            if prep_date_str:
                safety_record.safely_anchor_time = prep_date_str

            storage = str(request.data.get("storage_condition") or "Ambient")
            is_market_vendor = (
                listing.merchant
                and "market" in str(listing.merchant.merch_type or "").lower()
            )
            if is_market_vendor:
                safety_record.posted_temp = float(current_temp)
            elif "Frozen" in storage:
                safety_record.posted_temp = -18.00
            elif "Chilled" in storage:
                safety_record.posted_temp = 4.00
            else:
                safety_record.posted_temp = float(current_temp)

            parsed_exp = parse_frontend_expiry(
                request.data.get("expiry_date"), request.data.get("expiry_time")
            )
            if parsed_exp:
                safety_record.estimated_expiry = parsed_exp

            safety_record.save()

            try:
                commerce = safety_record.commerce

                raw_qty = request.data.get("quantity") or request.data.get(
                    "qty_available"
                )
                if raw_qty not in (None, ""):
                    commerce.qty_available = int(float(raw_qty))

                orig_price = request.data.get("orig_price") or request.data.get(
                    "original_price"
                )
                if orig_price not in (None, ""):
                    commerce.orig_price = orig_price

                disc_price = request.data.get(
                    "discounted_price"
                ) or request.data.get("price")
                if disc_price not in (None, ""):
                    commerce.discounted_price = disc_price
                    commerce.current_price = disc_price

                floor_price = request.data.get("floor_price")
                if floor_price not in (None, ""):
                    commerce.floor_price = floor_price

                new_status = request.data.get("status")
                if new_status:
                    commerce.status = new_status

                if safety_record.estimated_expiry:
                    exp_dt = safety_record.estimated_expiry
                    if timezone.is_naive(exp_dt):
                        exp_dt = timezone.make_aware(exp_dt)
                    if (exp_dt - timezone.now()).total_seconds() <= 1830:
                        commerce.status = "Flagged"

                commerce.save()
            except Exception as e:
                print("Error updating commerce record on edit:", e)

        return Response(
            {
                "message": "Listing updated successfully",
                "data": PerishableProductSerializer(listing).data,
            },
            status=status.HTTP_200_OK,
        )


class DeleteListingView(APIView):
    """
    Prevents removing a listing if there are any Pending customer orders tied to it.
    """

    authentication_classes = []
    permission_classes = [AllowAny]

    def delete(self, request, pk):
        try:
            listing = PerishableProduct.objects.get(pk=pk)

            has_pending_orders = OrderReservation.objects.filter(
                listing__listing__product=listing,
                order_status="Pending",
            ).exists()

            if has_pending_orders:
                return Response(
                    {
                        "error": (
                            f"Cannot remove '{listing.prod_name}' because there are pending customer orders "
                            f"awaiting pickup. Please complete or resolve all pending orders first."
                        )
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

            listing.delete()
            return Response(
                {"message": "Listing successfully deleted!"},
                status=status.HTTP_200_OK,
            )
        except PerishableProduct.DoesNotExist:
            return Response(
                {"error": "Listing not found."}, status=status.HTTP_404_NOT_FOUND
            )
        except Exception as e:
            return Response(
                {"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class EstablishmentNotificationsView(APIView):
    """
    Returns merchant operational notifications strictly sorted from latest to oldest (descending sort_ts).
    """

    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        events = []
        merchant = resolve_merchant(
            request.GET.get("merchant"), request.GET.get("email")
        )
        if not merchant:
            return Response([], status=status.HTTP_200_OK)

        auto_flag_expiring_listings(merchant=merchant)

        now = timezone.now()
        now_ts = now.timestamp()

        recent_orders = (
            OrderReservation.objects.filter(
                listing__listing__product__merchant=merchant
            )
            .select_related("consumer", "listing__listing__product")
            .order_by("-ordered_at", "-order_id")[:35]
        )

        seen_vouchers = set()
        for o in recent_orders:
            b_code = base_voucher_code(o.qr_voucher_code)
            v_key = f"{b_code}-{o.order_status}"
            if v_key in seen_vouchers:
                continue
            seen_vouchers.add(v_key)

            prod_name = (
                o.listing.listing.product.prod_name
                if (o.listing and o.listing.listing and o.listing.listing.product)
                else "Surplus Item"
            )
            buyer_name = o.consumer.cons_fullname if o.consumer else "A consumer"
            o_dt = timezone.localtime(o.ordered_at) if o.ordered_at else now
            time_str = o_dt.strftime("%b %d, %Y • %I:%M %p")
            sort_ts = float(o_dt.timestamp()) + (float(o.order_id) * 0.001)

            if o.order_status == "Pending":
                events.append(
                    {
                        "id": f"order-pend-{b_code}",
                        "type": "order",
                        "title": f"New Customer Reservation ({b_code})",
                        "description": f"{buyer_name} reserved {prod_name} (Voucher {b_code}, 20% Deposit Paid). Tap to review order in Orders.",
                        "time": time_str,
                        "sort_ts": sort_ts,
                        "target_route": "/establishment/orders",
                        "read": False,
                    }
                )
            elif o.order_status == "Completed":
                events.append(
                    {
                        "id": f"order-done-{b_code}",
                        "type": "order",
                        "title": f"Order Pickup Completed ({b_code})",
                        "description": f"{buyer_name} picked up order {b_code}. Net wallet share credited to your Active Wallet.",
                        "time": time_str,
                        "sort_ts": sort_ts + 1.0,
                        "target_route": "/establishment/earnings",
                        "read": False,
                    }
                )
            elif o.order_status == "No-Show":
                events.append(
                    {
                        "id": f"order-noshow-{b_code}",
                        "type": "system",
                        "title": f"Order Marked No-Show ({b_code})",
                        "description": f"Unclaimed order {b_code} ({buyer_name}) marked No-Show. Net share of 20% deposit credited to your wallet.",
                        "time": time_str,
                        "sort_ts": sort_ts + 2.0,
                        "target_route": "/establishment/orders",
                        "read": False,
                    }
                )

        recent_reviews = (
            RatingReview.objects.filter(
                surplus_listing__listing__product__merchant=merchant
            )
            .select_related("consumer", "surplus_listing__listing__product")
            .order_by("-review_id")[:15]
        )

        for rev in recent_reviews:
            p_name = (
                rev.surplus_listing.listing.product.prod_name
                if (rev.surplus_listing and rev.surplus_listing.listing)
                else "Food Listing"
            )
            c_name = rev.consumer.cons_fullname if rev.consumer else "Consumer"
            stars = "★" * int(rev.rating_score or 5)
            r_dt = (
                timezone.localtime(rev.created_at)
                if getattr(rev, "created_at", None)
                else now
            )
            events.append(
                {
                    "id": f"review-{rev.review_id}",
                    "type": "review",
                    "title": f"New Customer Rating {stars} ({rev.rating_score}/5)",
                    "description": f"{c_name} rated '{p_name}': \"{rev.review_comment or 'Great food rescue experience!'}\"",
                    "time": r_dt.strftime("%b %d, %Y • %I:%M %p"),
                    "sort_ts": float(r_dt.timestamp())
                    + (float(rev.review_id) * 0.001),
                    "target_route": "/establishment/impact",
                    "read": False,
                }
            )

        active_safety_qs = ListingSafety.objects.filter(
            product__merchant=merchant, estimated_expiry__isnull=False
        ).select_related("product", "commerce")

        for safety in active_safety_qs:
            if safety.estimated_expiry:
                expiry_dt = safety.estimated_expiry
                if timezone.is_naive(expiry_dt):
                    expiry_dt = timezone.make_aware(expiry_dt)

                seconds_remaining = (expiry_dt - now).total_seconds()
                if seconds_remaining < 3600:
                    prod_name = (
                        safety.product.prod_name
                        if safety.product
                        else "Surplus Item"
                    )
                    if seconds_remaining <= 1830:
                        flag_ts = min(
                            now_ts,
                            float(
                                (expiry_dt - timedelta(minutes=30)).timestamp()
                            ),
                        )
                        events.append(
                            {
                                "id": f"expiry-flag-{safety.pk}",
                                "type": "suggested_expiry",
                                "title": "Listing Flagged / Reached Safety Limit",
                                "description": f"Your listing '{prod_name}' reached its 30-min TTT safety threshold and was flagged.",
                                "time": timezone.localtime(expiry_dt).strftime(
                                    "%b %d, %Y • %I:%M %p"
                                ),
                                "sort_ts": flag_ts,
                                "target_route": "/establishment/listing",
                                "read": False,
                            }
                        )
                    else:
                        mins_left = max(1, int(seconds_remaining / 60))
                        warn_ts = float(
                            (
                                expiry_dt - timedelta(minutes=mins_left)
                            ).timestamp()
                        )
                        events.append(
                            {
                                "id": f"expiry-1hr-{safety.pk}",
                                "type": "expiry_1hr",
                                "title": "1-Hour Expiry Warning",
                                "description": f"Your listing '{prod_name}' will reach its expiry window in {mins_left} mins. Consider donating it to a Food Bank!",
                                "time": f"In ~{mins_left} mins ({timezone.localtime(expiry_dt).strftime('%I:%M %p')})",
                                "sort_ts": warn_ts,
                                "target_route": "/establishment/donation",
                                "read": False,
                            }
                        )

        my_donations = (
            Donation.objects.filter(
                merchant=merchant,
                status__in=["Claimed", "Ongoing", "Completed", "Successful"],
            )
            .select_related("food_bank", "listing__listing__product")
            .order_by("-updated_at", "-donation_id")[:15]
        )

        for don in my_donations:
            fb_name = (
                don.food_bank.org_name if don.food_bank else "Partner Food Bank"
            )
            p_name = (
                don.listing.listing.product.prod_name
                if (
                    don.listing
                    and don.listing.listing
                    and don.listing.listing.product
                )
                else "Surplus Donation"
            )
            d_dt = timezone.localtime(don.updated_at or don.created_at or now)
            events.append(
                {
                    "id": f"don-claim-{don.donation_id}-{don.status}",
                    "type": "donation",
                    "title": (
                        "Donation Claimed by Food Bank!"
                        if don.status in ("Claimed", "Ongoing")
                        else "Donation Received by Food Bank"
                    ),
                    "description": f"{fb_name} {don.status.lower()} your donation of {don.qty_donated}x {p_name}.",
                    "time": d_dt.strftime("%b %d, %Y • %I:%M %p"),
                    "sort_ts": float(d_dt.timestamp())
                    + (float(don.donation_id) * 0.001),
                    "target_route": "/establishment/donation",
                    "read": False,
                }
            )

        direct_notifs = (
            AdminNotification.objects.filter(merchant=merchant)
            .exclude(admin_notif_type__startswith="Broadcast")
            .order_by("-pk")[:25]
        )

        for an in direct_notifs:
            an_dt = (
                timezone.localtime(an.created_at)
                if getattr(an, "created_at", None)
                else now
            )
            notif_type_clean = str(an.admin_notif_type or "Compliance").strip()
            if notif_type_clean == "Report":
                title_str = "Consumer Report Alert"
                target_rt = "/establishment/listing"
            elif notif_type_clean == "Payout":
                title_str = "Automated Xendit Payout Settled"
                target_rt = "/establishment/earnings"
            elif notif_type_clean == "Listing":
                title_str = "Listing Activity Notice"
                target_rt = "/establishment/listing"
            else:
                title_str = f"Admin Notice ({notif_type_clean})"
                target_rt = "/establishment/profile"

            events.append(
                {
                    "id": f"compliance-{an.pk}",
                    "type": "system",
                    "title": title_str,
                    "description": an.message,
                    "time": an_dt.strftime("%b %d, %Y • %I:%M %p"),
                    "sort_ts": float(an_dt.timestamp())
                    + (float(an.pk) * 0.001),
                    "target_route": target_rt,
                    "read": False,
                }
            )

        events.sort(key=lambda x: float(x.get("sort_ts") or 0), reverse=True)
        return Response(events, status=status.HTTP_200_OK)


class EstablishmentAnnouncementsView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        merchant = resolve_merchant(
            request.GET.get("merchant"), request.GET.get("email")
        )
        broadcasts_qs = AdminNotification.objects.filter(
            admin_notif_type__startswith="Broadcast"
        ).order_by("-pk")

        if merchant and merchant.user and merchant.user.created_at:
            start_of_reg_day = merchant.user.created_at.replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            broadcasts_qs = (
                broadcasts_qs.filter(created_at__gte=start_of_reg_day)
                | broadcasts_qs.filter(created_at__isnull=True)
            ).order_by("-pk")

        announcements = []
        for b in broadcasts_qs[:25]:
            audience = (
                b.admin_notif_type.replace("Broadcast:", "").strip()
                or "All Users (System-wide)"
            )
            if audience not in ["All Users (System-wide)", "Merchants Only"]:
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
                    "sort_ts": float(b_dt.timestamp()) + (float(b.pk) * 0.001),
                }
            )

        announcements.sort(key=lambda x: float(x.get("sort_ts") or 0), reverse=True)
        return Response(announcements, status=status.HTTP_200_OK)


class MerchantPortalSummaryView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, merch_id):
        ensure_listing_reference_data()
        merchant = resolve_merchant(merch_id, request.GET.get("email"))
        if not merchant:
            return Response(
                {"error": "No merchant found"}, status=status.HTTP_404_NOT_FOUND
            )

        auto_flag_expiring_listings(merchant=merchant)

        now = timezone.now()
        products = (
            PerishableProduct.objects.filter(merchant=merchant)
            .select_related("category", "unit")
            .order_by("-prod_id")
        )

        gov = _get_normalized_gov()
        basic_comm_rate = Decimal(str(gov.get("commission_pct", 10.0))) / Decimal(
            "100"
        )
        pro_comm_rate = Decimal(str(gov.get("pro_commission_pct", 4.0))) / Decimal(
            "100"
        )
        is_pro_merchant = merchant.plan_id == 2
        active_comm_rate = pro_comm_rate if is_pro_merchant else basic_comm_rate

        shop_open_time, shop_close_time, shop_hours_enabled = (
            get_merchant_shop_hours(merchant)
        )
        is_shop_open, _, _ = is_merchant_currently_open(merchant)

        pending_orders = []
        completed_orders = []
        transactions_list = []
        recent_impact = []

        total_earnings_val = Decimal("0.00")
        saved_spoilage_val = Decimal("0.00")
        total_rescued_kg = Decimal("0.00")
        nearing_expiry_count = 0
        successful_rescues_count = 0
        seen_order_keys = set()

        merch_loc = (
            Location.objects.filter(user=merchant.user)
            .order_by("-is_primary", "-loc_id")
            .first()
        )

        reviews_qs = (
            RatingReview.objects.filter(
                surplus_listing__listing__product__merchant=merchant
            )
            .select_related("consumer", "surplus_listing__listing__product")
            .order_by("-review_id")
        )
        review_count = reviews_qs.count()
        avg_rating_val = reviews_qs.aggregate(avg=Avg("rating_score"))["avg"] or 0.0

        month_sequence = get_rolling_month_labels(6)
        monthly_earnings_map = {m_k: Decimal("0.00") for m_k in month_sequence}
        monthly_rescued_map = {m_k: 0.0 for m_k in month_sequence}

        orders_qs = list(
            OrderReservation.objects.filter(
                listing__listing__product__merchant=merchant
            )
            .select_related(
                "consumer__user",
                "listing__listing__product__category",
                "listing__listing__product__unit",
            )
            .order_by("-ordered_at", "order_id")
        )

        order_ids_all = [o.order_id for o in orders_qs]
        tx_map = {
            t.order_id: t
            for t in PaymentTransaction.objects.filter(order_id__in=order_ids_all)
        }

        grouped_orders = {}
        for o in orders_qs:
            commerce = o.listing
            safety = commerce.listing if commerce else None
            prod = safety.product if safety else None
            if not prod:
                continue

            seen_order_keys.add(prod.prod_id)
            b_code = base_voucher_code(o.qr_voucher_code)
            unit_name = prod.unit.unit_name if prod.unit else "pc"
            unit_short = (
                unit_name.split("(")[1].replace(")", "").strip()
                if "(" in unit_name and ")" in unit_name
                else unit_name
            )
            cat_name = prod.category.category_name if prod.category else "Surplus"

            (
                disc_price,
                orig_price,
                line_total,
                line_orig_total,
                deposit_paid,
                cash_due,
            ) = resolve_order_live_pricing(o, tx_map)

            pickup_date_str = (
                timezone.localtime(safety.estimated_expiry).strftime("%b %d, %Y")
                if safety and safety.estimated_expiry
                else "Today"
            )
            pickup_time_str = (
                timezone.localtime(safety.estimated_expiry).strftime("%I:%M %p")
                if safety and safety.estimated_expiry
                else "05:00 PM"
            )

            expiry_label = f"{pickup_date_str} • {pickup_time_str}"
            if safety and safety.estimated_expiry:
                exp_dt = safety.estimated_expiry
                if timezone.is_naive(exp_dt):
                    exp_dt = timezone.make_aware(exp_dt)
                secs_left = (exp_dt - now).total_seconds()
                if 0 < secs_left <= 7200 and o.order_status == "Pending":
                    nearing_expiry_count += 1
                if secs_left <= 0:
                    expiry_label = f"Expired ({pickup_date_str} {pickup_time_str})"
                elif secs_left < 3600:
                    expiry_label = (
                        f"{max(1, int(secs_left // 60))}m left ({pickup_time_str})"
                    )
                else:
                    expiry_label = f"{int(secs_left // 3600)}h left ({pickup_date_str} {pickup_time_str})"

            line_item_info = {
                "order_id": o.order_id,
                "prod_id": prod.prod_id,
                "food_name": prod.prod_name,
                "category": cat_name,
                "qty": o.order_qty,
                "unit": unit_short,
                "unit_price": f"₱{disc_price:,.2f}",
                "orig_price": f"₱{orig_price:,.2f}",
                "subtotal": f"₱{line_total:,.2f}",
                "image": prod.image_path
                or "https://images.unsplash.com/photo-1509440159596-0249088772ff",
                "expiry": f"{pickup_date_str} • {pickup_time_str}",
                "rescued_kg": float(o.rescued_weight_kg or 1.0),
            }

            if b_code not in grouped_orders:
                o_local = timezone.localtime(o.ordered_at) if o.ordered_at else now
                grouped_orders[b_code] = {
                    "id": b_code,
                    "order_id": o.order_id,
                    "prod_id": prod.prod_id,
                    "commerce_id": commerce.pk,
                    "customer": (
                        o.consumer.cons_fullname if o.consumer else "Consumer"
                    ),
                    "customer_phone": (
                        o.consumer.phone_num
                        if (o.consumer and o.consumer.phone_num)
                        else "Verified Buyer"
                    ),
                    "items_list": [line_item_info],
                    "total_qty_num": o.order_qty,
                    "unit_short": unit_short,
                    "ordered_dt": o_local,
                    "time": o_local.strftime("%b %d, %Y | %I:%M %p"),
                    "expiry": (
                        expiry_label
                        if o.order_status == "Pending"
                        else "Picked Up"
                    ),
                    "expiryDate": pickup_date_str,
                    "expiryTime": pickup_time_str,
                    "status": (
                        "Pending Pickup"
                        if o.order_status == "Pending"
                        else "Successful"
                    ),
                    "raw_status": o.order_status,
                    "image": prod.image_path
                    or "https://images.unsplash.com/photo-1509440159596-0249088772ff",
                    "establishment": merchant.bus_name,
                    "total_val_dec": line_total,
                    "orig_val_dec": line_orig_total,
                    "deposit_dec": deposit_paid,
                    "cash_dec": cash_due,
                    "category": cat_name,
                    "description": prod.prod_desc
                    or "Consumer rescue reservation.",
                    "preparationDate": (
                        safety.safely_anchor_time if safety else None
                    )
                    or pickup_date_str,
                    "storageCondition": "Standard",
                    "pickupDate": pickup_date_str,
                    "pickupTime": pickup_time_str,
                    "rescueVoucher": True,
                }
            else:
                grp = grouped_orders[b_code]
                grp["items_list"].append(line_item_info)
                grp["total_qty_num"] += o.order_qty
                grp["total_val_dec"] += line_total
                grp["orig_val_dec"] += line_orig_total
                grp["deposit_dec"] += deposit_paid
                grp["cash_dec"] += cash_due

        for b_code, grp in grouped_orders.items():
            items_list = grp["items_list"]
            if len(items_list) == 1:
                item_summary_title = items_list[0]["food_name"]
                qty_summary = f"{items_list[0]['qty']} {items_list[0]['unit']}"
            else:
                names_joined = ", ".join(
                    f"{i['qty']}x {i['food_name']}" for i in items_list
                )
                item_summary_title = f"{items_list[0]['food_name']} +{len(items_list) - 1} more ({names_joined})"
                qty_summary = f"{grp['total_qty_num']} items"

            total_p = grp["total_val_dec"]
            orig_p = grp["orig_val_dec"]
            dep_p = grp["deposit_dec"]
            cash_p = grp["cash_dec"]
            raw_st = grp["raw_status"]
            is_expired_flag = "Expired" in str(grp.get("expiry", ""))

            platform_fee_dec = (total_p * active_comm_rate).quantize(
                Decimal("0.01")
            )
            wallet_credit_dec = max(Decimal("0.00"), dep_p - platform_fee_dec)
            net_earn = (total_p - platform_fee_dec).quantize(Decimal("0.01"))

            display_status = (
                "Pending Pickup"
                if raw_st == "Pending"
                else ("No-Show" if raw_st == "No-Show" else "Successful")
            )

            card = {
                "id": b_code,
                "order_id": grp["order_id"],
                "prod_id": grp["prod_id"],
                "commerce_id": grp["commerce_id"],
                "customer": grp["customer"],
                "customer_phone": grp["customer_phone"],
                "item": item_summary_title,
                "items_breakdown": items_list,
                "qty": qty_summary,
                "raw_qty": grp["total_qty_num"],
                "time": grp["time"],
                "expiry": (
                    grp["expiry"]
                    if raw_st == "Pending"
                    else (
                        "Unclaimed (No-Show)"
                        if raw_st == "No-Show"
                        else "Picked Up"
                    )
                ),
                "expiryDate": grp["expiryDate"],
                "expiryTime": grp["expiryTime"],
                "is_past_expiry": bool(is_expired_flag and raw_st == "Pending"),
                "status": display_status,
                "image": grp["image"],
                "establishment": grp["establishment"],
                "unit_price": items_list[0]["unit_price"],
                "original_total": f"₱{orig_p:,.2f}",
                "total_price": f"₱{total_p:,.2f}",
                "deposit_paid": f"₱{dep_p:,.2f}",
                "platform_fee": f"₱{platform_fee_dec:,.2f}",
                "wallet_credit": f"₱{wallet_credit_dec:,.2f}",
                "cash_due": f"₱{cash_p:,.2f}",
                "price": f"₱{total_p:,.2f} (20% Paid: ₱{dep_p:,.2f} → Wallet: ₱{wallet_credit_dec:,.2f} | 80% Cash: ₱{cash_p:,.2f})",
                "category": grp["category"],
                "description": grp["description"],
                "preparationDate": grp["preparationDate"],
                "storageCondition": grp["storageCondition"],
                "pickupDate": grp["pickupDate"],
                "pickupTime": grp["pickupTime"],
                "rescueVoucher": True,
            }

            if raw_st == "Pending":
                pending_orders.append(card)
            elif raw_st in ("Completed", "No-Show"):
                completed_orders.append(card)
                if raw_st == "Completed":
                    total_earnings_val += net_earn
                    saved_spoilage_val += max(Decimal("0.00"), orig_p - total_p)

                    m_label = grp["ordered_dt"].strftime("%b")
                    if m_label in monthly_earnings_map:
                        monthly_earnings_map[m_label] += net_earn
                    if m_label in monthly_rescued_map:
                        monthly_rescued_map[m_label] += sum(
                            float(x.get("rescued_kg", 1.0)) for x in items_list
                        )

                    transactions_list.append(
                        {
                            "id": b_code,
                            "customer": grp["customer"],
                            "date": grp["time"],
                            "food": item_summary_title,
                            "items_breakdown": items_list,
                            "qty": qty_summary,
                            "original": f"₱{orig_p:,.2f}",
                            "discounted": f"₱{total_p:,.2f}",
                            "deposit_paid": f"₱{dep_p:,.2f}",
                            "wallet_credit": f"₱{wallet_credit_dec:,.2f}",
                            "cash_collected": f"₱{cash_p:,.2f}",
                            "commission_fee": f"₱{platform_fee_dec:,.2f}",
                            "net": f"₱{net_earn:,.2f}",
                            "total": f"₱{net_earn:,.2f}",
                        }
                    )

        for prod in products:
            safety = ListingSafety.objects.filter(product=prod).first()
            commerce = getattr(safety, "commerce", None) if safety else None
            if not commerce:
                continue

            unit_label = prod.unit.unit_name if prod.unit else "pc"
            unit_short = (
                unit_label.split("(")[1].replace(")", "").strip()
                if "(" in unit_label and ")" in unit_label
                else unit_label
            )

            disc_price = Decimal(
                str(commerce.current_price or commerce.discounted_price or 0)
            )
            orig_price = Decimal(str(commerce.orig_price or disc_price))
            qty_sold = int(commerce.qty_sold or 0)

            pickup_date_str = (
                timezone.localtime(safety.estimated_expiry).strftime("%b %d, %Y")
                if safety and safety.estimated_expiry
                else "Today"
            )

            if qty_sold > 0:
                successful_rescues_count += qty_sold

                raw_weight = Decimal(str(prod.weight_per_unit or 0))
                if prod.unit_id == 1:
                    unit_kg = (
                        (raw_weight / Decimal("1000"))
                        if raw_weight > 0
                        else Decimal("0.1")
                    )
                elif prod.unit_id == 2:
                    unit_kg = raw_weight if raw_weight > 0 else Decimal("1.0")
                else:
                    unit_kg = raw_weight if raw_weight > 0 else Decimal("1.0")

                item_kg = unit_kg * Decimal(qty_sold)
                total_rescued_kg += item_kg

                if prod.prod_id not in seen_order_keys:
                    gross_total = (disc_price * Decimal(qty_sold)).quantize(
                        Decimal("0.01")
                    )
                    reservation_fee_20pct = (
                        gross_total * Decimal("0.20")
                    ).quantize(Decimal("0.01"))
                    commission_fee = (gross_total * active_comm_rate).quantize(
                        Decimal("0.01")
                    )
                    wallet_credit = max(
                        Decimal("0.00"), reservation_fee_20pct - commission_fee
                    )
                    net_unit_earning = (
                        disc_price * (Decimal("1.00") - active_comm_rate)
                    ).quantize(Decimal("0.01"))
                    net_line_total = (gross_total - commission_fee).quantize(
                        Decimal("0.01")
                    )
                    spoilage_saved = max(
                        Decimal("0.00"),
                        (orig_price - disc_price) * Decimal(qty_sold),
                    )

                    total_earnings_val += net_line_total
                    saved_spoilage_val += spoilage_saved

                    transactions_list.append(
                        {
                            "id": f"OID{900 + prod.prod_id}",
                            "customer": "Verified Consumer",
                            "date": pickup_date_str,
                            "food": prod.prod_name,
                            "items_breakdown": [
                                {
                                    "food_name": prod.prod_name,
                                    "qty": qty_sold,
                                    "unit": unit_short,
                                    "unit_price": f"₱{disc_price:,.2f}",
                                    "subtotal": f"₱{gross_total:,.2f}",
                                }
                            ],
                            "qty": f"{qty_sold} {unit_short}",
                            "original": f"₱{(orig_price * Decimal(qty_sold)):,.2f}",
                            "discounted": f"₱{gross_total:,.2f}",
                            "deposit_paid": f"₱{reservation_fee_20pct:,.2f}",
                            "wallet_credit": f"₱{wallet_credit:,.2f}",
                            "cash_collected": f"₱{(gross_total - reservation_fee_20pct):,.2f}",
                            "commission_fee": f"₱{commission_fee:,.2f}",
                            "net": f"₱{net_unit_earning:,.2f}",
                            "total": f"₱{net_line_total:,.2f}",
                        }
                    )

                recent_impact.append(
                    {
                        "id": f"imp-prod-{prod.prod_id}",
                        "icon": "restaurant-outline",
                        "title": f"{prod.prod_name} sold & rescued",
                        "description": f"{qty_sold} {unit_short} ({float(item_kg):,.1f} kg) claimed by consumers.",
                        "date": pickup_date_str,
                    }
                )

        donations_qs = Donation.objects.filter(merchant=merchant).order_by(
            "-donation_id"
        )
        for don in donations_qs:
            if str(don.status).lower() in [
                "claimed",
                "claimed by food bank",
                "fulfilled",
                "completed",
                "delivered",
                "successful",
            ]:
                don_qty = int(don.qty_donated or 1)
                successful_rescues_count += 1
                total_rescued_kg += Decimal(don_qty) * Decimal("1.0")
                don_dt = don.updated_at or don.created_at or now
                don_month = timezone.localtime(don_dt).strftime("%b")
                if don_month in monthly_rescued_map:
                    monthly_rescued_map[don_month] += float(don_qty)
                recent_impact.append(
                    {
                        "id": f"imp-don-{don.donation_id}",
                        "icon": "heart-outline",
                        "title": "Donation Fulfilled",
                        "description": f"{don_qty} kg/units received by partner food bank.",
                        "date": timezone.localtime(don_dt).strftime("%b %d, %Y"),
                    }
                )

        withdrawals_list = []
        w_reqs = WithdrawalReq.objects.filter(merchant=merchant).order_by(
            "-withdrawal_id"
        )
        for w in w_reqs:
            amt_v = float(w.amount or 0)
            fee_v = float(w.fee_deducted or 0)
            net_v = float(w.net_payout or amt_v)
            withdrawals_list.append(
                {
                    "id": f"PO-{w.withdrawal_id}",
                    "withdrawal_id": w.withdrawal_id,
                    "amount": f"₱{amt_v:,.2f}",
                    "raw_amount": amt_v,
                    "fee_deducted": f"₱{fee_v:,.2f}",
                    "net_payout": f"₱{net_v:,.2f}",
                    "channel": w.channel or "GCash",
                    "account_number": w.gcash_num or merchant.phone or "",
                    "account_name": w.account_name
                    or merchant.contact_person
                    or merchant.bus_name,
                    "reference_number": w.reference_number or "",
                    "status": w.withdrawal_status or "Settled",
                    "created_at": (
                        timezone.localtime(w.created_at).strftime(
                            "%b %d, %Y • %I:%M %p"
                        )
                        if getattr(w, "created_at", None)
                        else "Recent"
                    ),
                    "processed_at": (
                        timezone.localtime(w.processed_at).strftime(
                            "%b %d, %Y • %I:%M %p"
                        )
                        if getattr(w, "processed_at", None)
                        else ""
                    ),
                }
            )

        wallet_bal = Decimal(str(merchant.wallet_bal or 0))
        if total_earnings_val == 0 and wallet_bal > 0:
            total_earnings_val = wallet_bal

        cur_month_key = now.strftime("%b")
        if (
            cur_month_key in monthly_earnings_map
            and monthly_earnings_map[cur_month_key] == 0
            and total_earnings_val > 0
        ):
            monthly_earnings_map[cur_month_key] = total_earnings_val

        monthly_earnings_chart = [
            {
                "month": m_k,
                "amount": float(monthly_earnings_map.get(m_k, Decimal("0.00"))),
            }
            for m_k in month_sequence
        ]

        rescued_kg_float = float(total_rescued_kg)
        co2_avoided_float = round(rescued_kg_float * 2.0, 1)
        people_helped_count = int(successful_rescues_count)
        portions_rescued_count = int(round(rescued_kg_float * 2))

        if (
            cur_month_key in monthly_rescued_map
            and sum(monthly_rescued_map.values()) == 0
            and rescued_kg_float > 0
        ):
            monthly_rescued_map[cur_month_key] = round(rescued_kg_float, 1)

        monthly_impact = [
            {
                "month": m_label,
                "rescued": round(monthly_rescued_map.get(m_label, 0.0), 1),
            }
            for m_label in month_sequence
        ]

        return Response(
            {
                "profile": {
                    "merch_id": merchant.merch_id,
                    "bus_name": merchant.bus_name,
                    "merch_type": merchant.merch_type or "Bakeshop",
                    "email": merchant.user.user_email,
                    "phone": merchant.phone or merchant.hotline or "Not Provided",
                    "hotline": merchant.hotline or "N/A",
                    "contact_person": merchant.contact_person or "Manager",
                    "address": merchant.address or "Cebu City, Philippines",
                    "location_locked": True,
                    "shop_open_time": shop_open_time,
                    "shop_close_time": shop_close_time,
                    "shop_hours_enabled": shop_hours_enabled,
                    "is_shop_open": is_shop_open,
                    "status": merchant.status,
                    "expiry_date": str(merchant.bus_expiry_date or ""),
                    "plan_id": merchant.plan_id,
                    "plan_name": (
                        f"Pro Tier (₱{gov['premium_monthly_price']:,.2f}/mo — 4% Fee)"
                        if merchant.plan_id == 2
                        else f"Basic Tier (Free — {gov['commission_pct']:.0f}% Fee)"
                    ),
                    "weekly_listings_used": len(products),
                    "weekly_listings_limit": (
                        "Unlimited"
                        if merchant.plan_id == 2
                        else str(gov["free_cap"])
                    ),
                    "deposit_pct": gov.get("deposit_pct", 20.0),
                    "commission_pct": gov.get("commission_pct", 10.0),
                    "basic_commission_pct": gov.get("commission_pct", 10.0),
                    "pro_commission_pct": gov.get("pro_commission_pct", 4.0),
                    "premium_monthly_price": gov["premium_monthly_price"],
                    "min_withdrawal": gov.get("min_withdrawal", 100.0),
                    "radius_km": gov["radius_km"],
                    "latitude": float(merch_loc.latitude) if merch_loc else 10.3157,
                    "longitude": (
                        float(merch_loc.longitude) if merch_loc else 123.8854
                    ),
                    "avg_rating": (
                        round(float(avg_rating_val), 1)
                        if review_count > 0
                        else 0.0
                    ),
                    "review_count": review_count,
                    "reviews": [
                        {
                            "id": r.review_id,
                            "consumer": (
                                r.consumer.cons_fullname
                                if r.consumer
                                else "Verified Consumer"
                            ),
                            "item": (
                                r.surplus_listing.listing.product.prod_name
                                if (
                                    r.surplus_listing
                                    and r.surplus_listing.listing
                                )
                                else "Surplus Food"
                            ),
                            "score": r.rating_score,
                            "comment": r.review_comment or "",
                            "date": (
                                timezone.localtime(r.created_at).strftime(
                                    "%b %d, %Y • %I:%M %p"
                                )
                                if getattr(r, "created_at", None)
                                else "Recent"
                            ),
                        }
                        for r in reviews_qs[:20]
                    ],
                },
                "orders": {
                    "active_pickups_count": len(pending_orders),
                    "nearing_expiry_count": nearing_expiry_count,
                    "pending_orders": pending_orders,
                    "completed_orders": completed_orders,
                },
                "earnings": {
                    "total_earnings": f"₱{float(total_earnings_val):,.2f}",
                    "raw_total_earnings": float(total_earnings_val),
                    "wallet_balance": f"₱{float(wallet_bal):,.2f}",
                    "raw_wallet_balance": float(wallet_bal),
                    "saved_on_spoilage": f"₱{float(saved_spoilage_val):,.2f}",
                    "transactions_completed": len(completed_orders),
                    "monthly_earnings": monthly_earnings_chart,
                    "transactions": transactions_list,
                    "withdrawals": withdrawals_list,
                },
                "impact": {
                    "food_rescued_kg": f"{rescued_kg_float:,.1f}",
                    "people_helped": f"{people_helped_count:,}",
                    "successful_rescues": f"{successful_rescues_count:,}",
                    "co2_avoided_kg": f"{co2_avoided_float:,.1f}",
                    "portions_rescued": f"{portions_rescued_count:,}",
                    "monthly_data": monthly_impact,
                    "recent_impact": recent_impact[:6],
                },
            },
            status=status.HTTP_200_OK,
        )

    def post(self, request, merch_id):
        ensure_listing_reference_data()
        merchant = resolve_merchant(merch_id, request.data.get("email"))
        if not merchant:
            return Response(
                {"error": "Merchant not found"}, status=status.HTTP_404_NOT_FOUND
            )

        action = request.data.get("action")

        if action == "update_profile":
            if "bus_name" in request.data and str(request.data["bus_name"]).strip():
                merchant.bus_name = str(request.data["bus_name"]).strip()
            if "phone" in request.data and str(request.data["phone"]).strip():
                merchant.phone = str(request.data["phone"]).strip()
            if "hotline" in request.data:
                merchant.hotline = str(request.data["hotline"]).strip()
            if (
                "contact_person" in request.data
                and str(request.data["contact_person"]).strip()
            ):
                merchant.contact_person = str(
                    request.data["contact_person"]
                ).strip()
            merchant.save()

            target_email = (
                str(
                    request.data.get("new_email")
                    or request.data.get("email")
                    or ""
                )
                .strip()
                .lower()
            )
            if target_email and target_email != merchant.user.user_email.lower():
                if (
                    User.objects.exclude(user_id=merchant.user.user_id)
                    .filter(user_email__iexact=target_email)
                    .exists()
                ):
                    return Response(
                        {
                            "error": "That email address is already registered to another account."
                        },
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                merchant.user.user_email = target_email

            raw_open = request.data.get("shop_open_time") or request.data.get(
                "shopOpenTime"
            )
            raw_close = request.data.get("shop_close_time") or request.data.get(
                "shopCloseTime"
            )
            raw_enabled = (
                request.data.get("shop_hours_enabled")
                if "shop_hours_enabled" in request.data
                else request.data.get("shopHoursEnabled")
            )

            if raw_open or raw_close or raw_enabled is not None:
                try:
                    cfg = json.loads(merchant.user.settings_json or "{}")
                    if not isinstance(cfg, dict):
                        cfg = {}
                except Exception:
                    cfg = {}
                if raw_open and str(raw_open).strip():
                    cfg["shop_open_time"] = str(raw_open).strip()
                    cfg["shopOpenTime"] = str(raw_open).strip()
                if raw_close and str(raw_close).strip():
                    cfg["shop_close_time"] = str(raw_close).strip()
                    cfg["shopCloseTime"] = str(raw_close).strip()
                if raw_enabled is not None:
                    cfg["shop_hours_enabled"] = bool(raw_enabled)
                    cfg["shopHoursEnabled"] = bool(raw_enabled)
                merchant.user.settings_json = json.dumps(cfg)

            merchant.user.save()
            is_open, open_str, close_str = is_merchant_currently_open(merchant)

            return Response(
                {
                    "message": "Storefront profile & operating hours updated! (Registered store location remains locked for KYB compliance.)",
                    "bus_name": merchant.bus_name,
                    "email": merchant.user.user_email,
                    "shop_open_time": open_str,
                    "shop_close_time": close_str,
                    "is_shop_open": is_open,
                },
                status=status.HTTP_200_OK,
            )

        elif action == "change_password":
            current_pw = str(request.data.get("current_password") or "").strip()
            new_password = str(request.data.get("new_password") or "").strip()
            if len(new_password) < 6:
                return Response(
                    {"error": "Password must be at least 6 characters."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            if current_pw and merchant.user.user_pass:
                is_valid = check_password(
                    current_pw, merchant.user.user_pass
                ) or (current_pw == merchant.user.user_pass)
                if not is_valid:
                    return Response(
                        {"error": "Your current password is incorrect."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )

            merchant.user.user_pass = make_password(new_password)
            merchant.user.save()
            return Response(
                {"message": "Password updated successfully!"},
                status=status.HTTP_200_OK,
            )

        elif action == "deactivate_account":
            merchant.status = "Inactive"
            merchant.status_reason = (
                "Storefront account deactivated/deleted by merchant"
            )
            merchant.is_verified = False
            merchant.save()
            merchant.user.is_banned = True
            merchant.user.save()

            AdminNotification.objects.create(
                user_id=1,
                admin_notif_type="Compliance",
                message=f"Merchant MID-{merchant.merch_id} ({merchant.bus_name}) deactivated their account (Status set to Inactive).",
                merchant=merchant,
            )
            return Response(
                {"message": "Your storefront account has been set to Inactive."},
                status=status.HTTP_200_OK,
            )

        elif action == "preview_order":
            raw_code = base_voucher_code(request.data.get("order_code", ""))
            prod_id = request.data.get("prod_id")
            now = timezone.now()

            matching_orders = (
                list(
                    OrderReservation.objects.filter(
                        qr_voucher_code__istartswith=raw_code,
                        listing__listing__product__merchant=merchant,
                    ).select_related(
                        "listing__listing__product__category",
                        "listing__listing__product__unit",
                        "consumer",
                    )
                )
                if raw_code
                else []
            )

            if not matching_orders and prod_id:
                matching_orders = list(
                    OrderReservation.objects.filter(
                        listing__listing__product__prod_id=prod_id,
                        listing__listing__product__merchant=merchant,
                        order_status="Pending",
                    ).select_related(
                        "listing__listing__product__category",
                        "listing__listing__product__unit",
                        "consumer",
                    )
                )

            if not matching_orders:
                return Response(
                    {
                        "error": f"No matching order found for voucher '{raw_code}'."
                    },
                    status=status.HTTP_404_NOT_FOUND,
                )

            if all(o.order_status == "Completed" for o in matching_orders):
                return Response(
                    {
                        "already_completed": True,
                        "voucher_code": raw_code,
                        "error": f"Order {raw_code} has already been completed and picked up.",
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

            if any(o.order_status == "No-Show" for o in matching_orders):
                return Response(
                    {
                        "error": f"Voucher {raw_code} was already marked as an unclaimed No-Show."
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

            for o in matching_orders:
                saf = o.listing.listing if o.listing else None
                exp_dt = saf.estimated_expiry if saf else None
                if exp_dt:
                    if timezone.is_naive(exp_dt):
                        exp_dt = timezone.make_aware(exp_dt)
                    if exp_dt <= now:
                        exp_fmt = timezone.localtime(exp_dt).strftime(
                            "%b %d, %Y %I:%M %p"
                        )
                        return Response(
                            {
                                "error": (
                                    f"Food Safety Cutoff (RA 10611): Order {raw_code} passed its pickup deadline "
                                    f"({exp_fmt}) and can no longer be released. Please mark this order as 'No-Show' "
                                    f"to claim your wallet share."
                                )
                            },
                            status=status.HTTP_400_BAD_REQUEST,
                        )

            first_o = matching_orders[0]
            total_price = Decimal("0.00")
            deposit_paid = Decimal("0.00")
            total_qty = 0
            items_breakdown = []

            for o in matching_orders:
                comm = o.listing
                saf = comm.listing if comm else None
                prd = saf.product if saf else None
                u_name = prd.unit.unit_name if prd and prd.unit else "pc"
                u_short = (
                    u_name.split("(")[1].replace(")", "").strip()
                    if "(" in u_name and ")" in u_name
                    else u_name
                )
                d_price, _, l_tot, _, dep, _ = resolve_order_live_pricing(o)

                total_price += l_tot
                deposit_paid += dep
                total_qty += o.order_qty
                items_breakdown.append(
                    {
                        "food_name": prd.prod_name if prd else "Surplus Food",
                        "qty": o.order_qty,
                        "unit": u_short,
                        "unit_price": f"₱{d_price:,.2f}",
                        "subtotal": f"₱{l_tot:,.2f}",
                    }
                )

            gov = _get_normalized_gov()
            comm_rate = (
                Decimal(str(gov.get("pro_commission_pct", 4.0)))
                / Decimal("100")
                if merchant.plan_id == 2
                else Decimal(str(gov.get("commission_pct", 10.0)))
                / Decimal("100")
            )
            platform_fee = (total_price * comm_rate).quantize(Decimal("0.01"))
            wallet_credit = max(Decimal("0.00"), deposit_paid - platform_fee)
            cash_due = max(Decimal("0.00"), total_price - deposit_paid)

            first_saf = first_o.listing.listing if first_o.listing else None
            first_prd = first_saf.product if first_saf else None
            exp_date_str = (
                timezone.localtime(first_saf.estimated_expiry).strftime(
                    "%b %d, %Y"
                )
                if first_saf and first_saf.estimated_expiry
                else "Today"
            )
            exp_time_str = (
                timezone.localtime(first_saf.estimated_expiry).strftime(
                    "%I:%M %p"
                )
                if first_saf and first_saf.estimated_expiry
                else "05:00 PM"
            )

            item_title = (
                items_breakdown[0]["food_name"]
                if len(items_breakdown) == 1
                else f"{items_breakdown[0]['food_name']} +{len(items_breakdown) - 1} more"
            )

            return Response(
                {
                    "order": {
                        "id": base_voucher_code(first_o.qr_voucher_code),
                        "prod_id": first_prd.prod_id if first_prd else None,
                        "customer": (
                            first_o.consumer.cons_fullname
                            if first_o.consumer
                            else "Consumer"
                        ),
                        "customer_phone": (
                            first_o.consumer.phone_num
                            if (first_o.consumer and first_o.consumer.phone_num)
                            else "Verified Buyer"
                        ),
                        "item": item_title,
                        "items_breakdown": items_breakdown,
                        "category": (
                            first_prd.category.category_name
                            if (first_prd and first_prd.category)
                            else "Surplus"
                        ),
                        "qty": (
                            f"{total_qty} items"
                            if len(items_breakdown) > 1
                            else f"{items_breakdown[0]['qty']} {items_breakdown[0]['unit']}"
                        ),
                        "unit_price": items_breakdown[0]["unit_price"],
                        "total_price": f"₱{total_price:,.2f}",
                        "deposit_paid": f"₱{deposit_paid:,.2f}",
                        "platform_fee": f"₱{platform_fee:,.2f}",
                        "wallet_credit": f"₱{wallet_credit:,.2f}",
                        "cash_due": f"₱{cash_due:,.2f}",
                        "price": f"₱{total_price:,.2f} (20% Paid: ₱{deposit_paid:,.2f} → Wallet: ₱{wallet_credit:,.2f} | 80% Cash: ₱{cash_due:,.2f})",
                        "time": (
                            timezone.localtime(first_o.ordered_at).strftime(
                                "%b %d, %Y • %I:%M %p"
                            )
                            if first_o.ordered_at
                            else "Recent"
                        ),
                        "pickupDate": exp_date_str,
                        "pickupTime": exp_time_str,
                        "expiry": f"{exp_date_str} • {exp_time_str}",
                        "status": "Pending Pickup",
                        "image": (first_prd.image_path if first_prd else "")
                        or "https://images.unsplash.com/photo-1509440159596-0249088772ff",
                    }
                },
                status=status.HTTP_200_OK,
            )

        elif action == "confirm_order":
            prod_id = request.data.get("prod_id")
            raw_code = base_voucher_code(request.data.get("order_code", ""))
            now = timezone.now()

            all_matching = (
                list(
                    OrderReservation.objects.filter(
                        qr_voucher_code__istartswith=raw_code,
                        listing__listing__product__merchant=merchant,
                    ).select_related(
                        "listing__listing__product", "consumer__user"
                    )
                )
                if raw_code
                else []
            )

            if all_matching and all(
                o.order_status == "Completed" for o in all_matching
            ):
                return Response(
                    {
                        "already_completed": True,
                        "voucher_code": raw_code,
                        "error": f"Order {raw_code} has already been completed and picked up.",
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

            matching_orders = [
                o for o in all_matching if o.order_status == "Pending"
            ]

            if not matching_orders and prod_id:
                matching_orders = list(
                    OrderReservation.objects.filter(
                        listing__listing__product__prod_id=prod_id,
                        listing__listing__product__merchant=merchant,
                        order_status="Pending",
                    ).select_related(
                        "listing__listing__product", "consumer__user"
                    )
                )

            if not matching_orders:
                return Response(
                    {"error": "Matching pending voucher or order ID not found."},
                    status=status.HTTP_404_NOT_FOUND,
                )

            for order_res in matching_orders:
                saf = order_res.listing.listing if order_res.listing else None
                exp_dt = saf.estimated_expiry if saf else None
                if exp_dt:
                    if timezone.is_naive(exp_dt):
                        exp_dt = timezone.make_aware(exp_dt)
                    if exp_dt <= now:
                        exp_fmt = timezone.localtime(exp_dt).strftime(
                            "%b %d, %Y %I:%M %p"
                        )
                        return Response(
                            {
                                "error": (
                                    f"Food Safety Cutoff (RA 10611): Order {raw_code} passed its pickup deadline "
                                    f"({exp_fmt}) and cannot be released. Please mark it as 'No-Show' instead."
                                )
                            },
                            status=status.HTTP_400_BAD_REQUEST,
                        )

            gov = _get_normalized_gov()
            is_pro = merchant.plan_id == 2
            comm_pct = Decimal(
                str(
                    gov.get("pro_commission_pct", 4.0)
                    if is_pro
                    else gov.get("commission_pct", 10.0)
                )
            )
            comm_rate = comm_pct / Decimal("100")

            total_deposit = Decimal("0.00")
            total_gross = Decimal("0.00")
            total_platform_fee = Decimal("0.00")
            total_wallet_credit = Decimal("0.00")
            total_weight = Decimal("0.00")
            released_names = []
            consumer_obj = matching_orders[0].consumer

            with transaction.atomic():
                for order_res in matching_orders:
                    commerce = order_res.listing
                    prod = commerce.listing.product
                    qty_done = int(order_res.order_qty or 1)

                    commerce.qty_reserved = max(
                        0, (commerce.qty_reserved or 0) - qty_done
                    )
                    commerce.qty_sold = (commerce.qty_sold or 0) + qty_done
                    if (
                        commerce.qty_available == 0
                        and commerce.qty_reserved == 0
                    ):
                        commerce.status = "Sold Out"
                    commerce.save()

                    _, _, grs_amt, _, dep_amt, _ = resolve_order_live_pricing(
                        order_res
                    )
                    order_res.downpayment_amount = dep_amt
                    order_res.order_status = "Completed"
                    order_res.save()

                    line_platform_fee = (grs_amt * comm_rate).quantize(
                        Decimal("0.01")
                    )
                    line_wallet_credit = max(
                        Decimal("0.00"), dep_amt - line_platform_fee
                    )
                    line_merch_net = (grs_amt - line_platform_fee).quantize(
                        Decimal("0.01")
                    )

                    PaymentTransaction.objects.filter(order=order_res).update(
                        commission_taken=line_platform_fee,
                        merchant_earnings=line_merch_net,
                    )

                    total_deposit += dep_amt
                    total_gross += grs_amt
                    total_platform_fee += line_platform_fee
                    total_wallet_credit += line_wallet_credit
                    total_weight += Decimal(
                        str(order_res.rescued_weight_kg or 1)
                    )
                    released_names.append(f"{qty_done}x {prod.prod_name}")

                    StockLedger.objects.create(
                        listing=commerce,
                        transaction_type="Pickup_Confirmed",
                        qty_change=-qty_done,
                        reason=f"Verified voucher {raw_code}",
                    )

                if consumer_obj:
                    consumer_obj.total_food_saved = (
                        Decimal(str(consumer_obj.total_food_saved or 0))
                        + total_weight
                    )
                    consumer_obj.save()
                    if consumer_obj.user:
                        UserNotification.objects.create(
                            user=consumer_obj.user,
                            message=f"Pickup completed for Voucher {raw_code} ({', '.join(released_names)}) at {merchant.bus_name}! Tap 'My Claims' to rate and review your order.",
                        )

                cash_80pct = max(Decimal("0.00"), total_gross - total_deposit)
                merchant.wallet_bal = (
                    Decimal(str(merchant.wallet_bal or 0))
                    + total_wallet_credit
                )
                merchant.save()

                AdminNotification.objects.create(
                    user_id=1,
                    admin_notif_type="Reservation",
                    message=(
                        f"Order {raw_code} ({', '.join(released_names)}) completed at {merchant.bus_name}. "
                        f"20% Deposit: ₱{float(total_deposit):,.2f} → ReServe Commission ({int(comm_pct)}%): "
                        f"₱{float(total_platform_fee):,.2f} | Merchant Wallet Credit: ₱{float(total_wallet_credit):,.2f}."
                    ),
                    merchant=merchant,
                )

            wallet_pct_label = 16 if is_pro else 10
            return Response(
                {
                    "message": (
                        f"Voucher {raw_code} Released ({', '.join(released_names)})!\n"
                        f"• Collect 80% Cash at Counter: ₱{float(cash_80pct):,.2f}\n"
                        f"• 20% Online Deposit Paid: ₱{float(total_deposit):,.2f}\n"
                        f"• ReServe Fee ({int(comm_pct)}%): -₱{float(total_platform_fee):,.2f}\n"
                        f"• Credited to Your Wallet ({wallet_pct_label}%): +₱{float(total_wallet_credit):,.2f}"
                    )
                },
                status=status.HTTP_200_OK,
            )

        elif action == "request_withdrawal":
            gov = _get_normalized_gov()
            min_w = Decimal(str(gov.get("min_withdrawal", 100.00)))
            current_bal = Decimal(str(merchant.wallet_bal or 0))

            if current_bal < min_w:
                return Response(
                    {
                        "error": f"Minimum withdrawal is ₱{float(min_w):,.2f} (Current wallet balance: ₱{float(current_bal):,.2f})."
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

            raw_amt = request.data.get("amount")
            try:
                req_amt = (
                    Decimal(str(raw_amt)).quantize(Decimal("0.01"))
                    if raw_amt is not None
                    else current_bal
                )
            except Exception:
                return Response(
                    {"error": "Please enter a valid withdrawal amount."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            if req_amt < min_w:
                return Response(
                    {
                        "error": f"Requested withdrawal amount must be at least ₱{float(min_w):,.2f}."
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

            if req_amt > current_bal:
                return Response(
                    {
                        "error": f"Requested amount exceeds your available wallet balance (₱{float(current_bal):,.2f})."
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

            channel = str(request.data.get("channel", "GCash")).strip()
            account_number = str(
                request.data.get("account_number") or merchant.phone or ""
            ).strip()
            account_name = str(
                request.data.get("account_name")
                or merchant.contact_person
                or merchant.bus_name
            ).strip()

            if not account_number or not account_name:
                return Response(
                    {
                        "error": f"Please enter both your {channel} account number and account name."
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

            fee_deducted = Decimal("0.00")
            net_payout = req_amt

            ext_id = f"reserve-po-{merchant.merch_id}-{uuid.uuid4().hex[:8]}"
            xendit_ref = disburse_xendit_payout(
                channel=channel,
                account_number=account_number,
                account_name=account_name,
                net_amount_dec=net_payout,
                external_id=ext_id,
            )

            merchant.wallet_bal = max(Decimal("0.00"), current_bal - req_amt)
            merchant.save()

            now_ts = timezone.now()
            w_req = WithdrawalReq.objects.create(
                merchant=merchant,
                amount=req_amt,
                channel=channel,
                gcash_num=account_number,
                account_name=account_name,
                fee_deducted=fee_deducted,
                net_payout=net_payout,
                reference_number=xendit_ref,
                withdrawal_status="Settled",
                processed_at=now_ts,
            )

            if merchant.user:
                UserNotification.objects.create(
                    user=merchant.user,
                    message=f"Automated Xendit Payout PO-{w_req.withdrawal_id} Disbursed! 100% of requested ₱{float(net_payout):,.2f} (₱0 Payout Fee) sent to {channel} ({account_number}). Xendit Ref #: {xendit_ref}.",
                )

            AdminNotification.objects.create(
                user_id=1,
                admin_notif_type="Payout",
                message=f"Automated Xendit Payout PO-{w_req.withdrawal_id} settled for {merchant.bus_name}: ₱{float(net_payout):,.2f} (0% Payout Fee) via {channel} ({account_number}). Ref #: {xendit_ref}.",
                merchant=merchant,
                withdrawal=w_req,
            )

            return Response(
                {
                    "message": (
                        f"Automated Xendit Payout PO-{w_req.withdrawal_id} Disbursed!\n"
                        f"• Channel: {channel} ({account_number} - {account_name})\n"
                        f"• Amount Withdrawn: ₱{float(req_amt):,.2f}\n"
                        f"• Withdrawal Fee: ₱0.00 (Commission already settled per order)\n"
                        f"• Net Disbursed (100%): ₱{float(net_payout):,.2f}\n"
                        f"• Xendit Reference #: {xendit_ref}"
                    ),
                    "withdrawal_id": w_req.withdrawal_id,
                    "reference_number": xendit_ref,
                },
                status=status.HTTP_200_OK,
            )

        elif action == "upgrade_tier":
            gov = _get_normalized_gov()
            target_plan = int(request.data.get("plan_id", 2))

            if target_plan == 2:
                xendit_inv_id = str(
                    request.data.get("xendit_invoice_id") or ""
                ).strip()
                is_paid, inv_status, _, err_msg = verify_xendit_invoice_paid(
                    xendit_inv_id
                )
                if not is_paid:
                    return Response(
                        {
                            "error": err_msg
                            or f"Xendit subscription invoice is not yet paid (Current status: {inv_status}). Please complete your ₱{gov['premium_monthly_price']:,.2f} Xendit payment first."
                        },
                        status=status.HTTP_400_BAD_REQUEST,
                    )

            merchant.plan_id = target_plan
            merchant.save()
            plan_label = (
                f"Pro Tier (₱{gov['premium_monthly_price']:,.2f}/mo — Unlimited Listings, 4% Low Commission)"
                if target_plan == 2
                else f"Basic Tier (Free — {gov['free_cap']} Listings/Week, {gov['commission_pct']:.0f}% Commission)"
            )
            return Response(
                {
                    "message": f"Subscription updated to {plan_label}!",
                    "plan_id": merchant.plan_id,
                    "plan_name": (
                        "Pro Tier (₱149/mo)"
                        if merchant.plan_id == 2
                        else "Basic Tier (Free)"
                    ),
                },
                status=status.HTTP_200_OK,
            )

        return Response(
            {"error": "Invalid action"}, status=status.HTTP_400_BAD_REQUEST
        )


class ConsumerPortalView(APIView):
    permission_classes = [AllowAny]

    def _resolve_consumer(self, cons_id=None, email=None):
        if cons_id and str(cons_id).isdigit() and int(cons_id) > 0:
            c = (
                Consumer.objects.select_related("user")
                .filter(cons_id=int(cons_id))
                .first()
            )
            if c:
                return c
        if email:
            c = (
                Consumer.objects.select_related("user")
                .filter(user__user_email__iexact=str(email).strip())
                .first()
            )
            if c:
                return c
        return None

    def get(self, request, cons_id):
        auto_flag_expiring_listings()
        consumer = self._resolve_consumer(cons_id, request.GET.get("email"))
        if not consumer:
            return Response(
                {"error": "Consumer not found"}, status=status.HTTP_404_NOT_FOUND
            )

        gov = _get_normalized_gov()

        orders_qs = list(
            OrderReservation.objects.filter(consumer=consumer)
            .select_related(
                "listing__listing__product__merchant__user",
                "listing__listing__product__unit",
                "listing__listing__product__category",
            )
            .order_by("-ordered_at", "order_id")
        )

        order_ids_all = [o.order_id for o in orders_qs]
        tx_map = {
            t.order_id: t
            for t in PaymentTransaction.objects.filter(order_id__in=order_ids_all)
        }

        consumer_loc = (
            Location.objects.filter(user=consumer.user)
            .order_by("-is_primary", "-loc_id")
            .first()
        )

        consumer_reviews = {
            r.order_id: r
            for r in RatingReview.objects.filter(consumer=consumer)
            if r.order_id
        }

        grouped_claims = {}
        notifs_list = []

        for o in orders_qs:
            commerce = o.listing
            safety = commerce.listing if commerce else None
            prod = safety.product if safety else None
            merch = prod.merchant if prod else None

            b_code = base_voucher_code(o.qr_voucher_code)

            merch_lat, merch_lng = 10.3157, 123.8854
            if merch and merch.user:
                m_loc = (
                    Location.objects.filter(user=merch.user)
                    .order_by("-is_primary", "-loc_id")
                    .first()
                )
                if m_loc:
                    merch_lat, merch_lng = (
                        float(m_loc.latitude),
                        float(m_loc.longitude),
                    )

            (
                unit_price,
                orig_unit_price,
                total_val,
                orig_line_total,
                paid_deposit,
                remaining_bal,
            ) = resolve_order_live_pricing(o, tx_map)
            savings_val = max(Decimal("0.00"), orig_line_total - total_val)

            pickup_deadline_str = (
                timezone.localtime(safety.estimated_expiry).strftime(
                    "%b %d, %Y %I:%M %p"
                )
                if safety and safety.estimated_expiry
                else "Today, 5:00 PM"
            )
            o_local = (
                timezone.localtime(o.ordered_at)
                if o.ordered_at
                else timezone.now()
            )
            order_date_str = o_local.strftime("%b %d, %Y • %I:%M %p")

            existing_rev = consumer_reviews.get(o.order_id)
            if not existing_rev and commerce:
                existing_rev = RatingReview.objects.filter(
                    consumer=consumer, surplus_listing=commerce
                ).first()

            line_item = {
                "order_id": o.order_id,
                "prod_id": prod.prod_id if prod else None,
                "food_name": (
                    prod.prod_name if prod else f"Rescue Item #{o.order_id}"
                ),
                "qty": o.order_qty,
                "unit_price": f"{float(unit_price):.2f}",
                "orig_price": f"{float(orig_unit_price):.2f}",
                "subtotal": f"{float(total_val):.2f}",
                "image": (prod.image_path if prod else "")
                or "https://images.unsplash.com/photo-1509440159596-0249088772ff",
                "pickup_deadline": pickup_deadline_str,
            }

            if b_code not in grouped_claims:
                grouped_claims[b_code] = {
                    "order_id": o.order_id,
                    "order_ids": [o.order_id],
                    "prod_id": prod.prod_id if prod else None,
                    "commerce_id": commerce.pk if commerce else None,
                    "merchant_id": merch.merch_id if merch else None,
                    "voucher_code": b_code,
                    "status": o.order_status,
                    "total_qty": o.order_qty,
                    "items_breakdown": [line_item],
                    "category": (
                        prod.category.category_name
                        if (prod and prod.category)
                        else "Surplus"
                    ),
                    "description": prod.prod_desc if prod else "",
                    "image": (prod.image_path if prod else "")
                    or "https://images.unsplash.com/photo-1509440159596-0249088772ff",
                    "establishment": (
                        merch.bus_name if merch else "Partner Establishment"
                    ),
                    "address": (
                        merch.address
                        if merch and merch.address
                        else "Cebu City"
                    ),
                    "merchant_lat": merch_lat,
                    "merchant_lng": merch_lng,
                    "pickup_deadline": pickup_deadline_str,
                    "ordered_at": order_date_str,
                    "sort_ts": o_local.timestamp(),
                    "orig_val_dec": orig_line_total,
                    "total_val_dec": total_val,
                    "paid_dec": paid_deposit,
                    "rem_dec": remaining_bal,
                    "sav_dec": savings_val,
                    "is_reviewed": bool(existing_rev),
                    "rating_score": (
                        existing_rev.rating_score if existing_rev else 0
                    ),
                    "review_comment": (
                        existing_rev.review_comment if existing_rev else ""
                    ),
                }
            else:
                g = grouped_claims[b_code]
                g["order_ids"].append(o.order_id)
                g["total_qty"] += o.order_qty
                g["items_breakdown"].append(line_item)
                g["orig_val_dec"] += orig_line_total
                g["total_val_dec"] += total_val
                g["paid_dec"] += paid_deposit
                g["rem_dec"] += remaining_bal
                g["sav_dec"] += savings_val
                if existing_rev and not g["is_reviewed"]:
                    g["is_reviewed"] = True
                    g["rating_score"] = existing_rev.rating_score
                    g["review_comment"] = existing_rev.review_comment

        claims_list = []
        for b_code, g in grouped_claims.items():
            ib = g["items_breakdown"]
            if len(ib) == 1:
                food_summary = ib[0]["food_name"]
            else:
                food_summary = f"{ib[0]['food_name']} +{len(ib) - 1} more ({', '.join(str(x['qty']) + 'x ' + x['food_name'] for x in ib)})"

            claim_row = {
                "order_id": g["order_id"],
                "order_ids": g["order_ids"],
                "prod_id": g["prod_id"],
                "commerce_id": g["commerce_id"],
                "merchant_id": g["merchant_id"],
                "voucher_code": b_code,
                "status": g["status"],
                "qty": g["total_qty"],
                "food_name": food_summary,
                "items_breakdown": ib,
                "category": g["category"],
                "description": g["description"],
                "image": g["image"],
                "establishment": g["establishment"],
                "address": g["address"],
                "merchant_lat": g["merchant_lat"],
                "merchant_lng": g["merchant_lng"],
                "pickup_deadline": g["pickup_deadline"],
                "ordered_at": g["ordered_at"],
                "original_total": f"{float(g['orig_val_dec']):.2f}",
                "total_value": f"{float(g['total_val_dec']):.2f}",
                "paid_today": f"{float(g['paid_dec']):.2f}",
                "remaining_balance": f"{float(g['rem_dec']):.2f}",
                "savings": f"{float(g['sav_dec']):.2f}",
                "is_reviewed": g["is_reviewed"],
                "rating_score": g["rating_score"],
                "review_comment": g["review_comment"],
            }
            claims_list.append(claim_row)

            if g["status"] == "Pending":
                notifs_list.append(
                    {
                        "id": f"ord-pend-{b_code}",
                        "type": "order",
                        "icon": "checkmark-circle-outline",
                        "title": f"Reservation Confirmed ({b_code})",
                        "text": f"Your reservation for {food_summary} at {g['establishment']} is ready for pickup before {g['pickup_deadline']}.",
                        "time": g["ordered_at"],
                        "sort_ts": g["sort_ts"],
                        "target_route": f"/consumer/claims?voucher={b_code}",
                        "unread": True,
                    }
                )
            elif g["status"] == "Completed":
                notifs_list.append(
                    {
                        "id": f"ord-done-{b_code}",
                        "type": "order",
                        "icon": "pricetag-outline",
                        "title": f"Pickup Completed",
                        "text": f"You rescued {food_summary} and saved ₱{float(g['sav_dec']):.2f}! Tap to view summary & rate your experience.",
                        "time": g["ordered_at"],
                        "sort_ts": g["sort_ts"] + 1,
                        "target_route": f"/consumer/claims?voucher={b_code}",
                        "unread": False,
                    }
                )

        broadcasts_qs = AdminNotification.objects.filter(
            admin_notif_type__startswith="Broadcast"
        ).order_by("-pk")
        if consumer.user and consumer.user.created_at:
            start_reg = consumer.user.created_at.replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            broadcasts_qs = (
                broadcasts_qs.filter(created_at__gte=start_reg)
                | broadcasts_qs.filter(created_at__isnull=True)
            ).order_by("-pk")

        for b in broadcasts_qs[:15]:
            aud = b.admin_notif_type.replace("Broadcast:", "").strip()
            if aud in ["All Users (System-wide)", "Consumers Only"]:
                parts = (b.message or "").split(" || ", 1)
                b_local = (
                    timezone.localtime(b.created_at)
                    if getattr(b, "created_at", None)
                    else timezone.now()
                )
                notifs_list.append(
                    {
                        "id": f"bcast-{b.pk}",
                        "type": "announcement",
                        "icon": "megaphone-outline",
                        "title": (
                            parts[0]
                            if len(parts) > 1
                            else "System Announcement"
                        ),
                        "text": parts[1] if len(parts) > 1 else b.message,
                        "time": b_local.strftime("%b %d, %Y • %I:%M %p"),
                        "sort_ts": b_local.timestamp(),
                        "target_route": "/consumer/homepage",
                        "unread": True,
                    }
                )

        notifs_list.sort(key=lambda x: x.get("sort_ts", 0), reverse=True)

        return Response(
            {
                "profile": {
                    "cons_id": consumer.cons_id,
                    "full_name": consumer.cons_fullname,
                    "email": consumer.user.user_email,
                    "phone": consumer.phone_num or "",
                    "address": consumer.address or "Cebu City",
                    "total_food_saved": float(consumer.total_food_saved or 0),
                    "radius_km": gov["radius_km"],
                    "deposit_pct": gov.get("deposit_pct", 20.0),
                    "latitude": (
                        float(consumer_loc.latitude)
                        if consumer_loc
                        else 10.3157
                    ),
                    "longitude": (
                        float(consumer_loc.longitude)
                        if consumer_loc
                        else 123.8854
                    ),
                },
                "claims": claims_list,
                "notifications": notifs_list,
            },
            status=status.HTTP_200_OK,
        )

    def post(self, request, cons_id):
        auto_flag_expiring_listings()
        consumer = self._resolve_consumer(cons_id, request.data.get("email"))
        if not consumer:
            return Response(
                {"error": "Consumer account not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        action = request.data.get("action")

        if action == "update_profile":
            if (
                "full_name" in request.data
                and str(request.data["full_name"]).strip()
            ):
                consumer.cons_fullname = str(request.data["full_name"]).strip()
            if "phone" in request.data and str(request.data["phone"]).strip():
                consumer.phone_num = str(request.data["phone"]).strip()
            if (
                "address" in request.data
                and str(request.data["address"]).strip()
            ):
                consumer.address = str(request.data["address"]).strip()
            consumer.save()

            target_email = (
                str(
                    request.data.get("new_email")
                    or request.data.get("email")
                    or ""
                )
                .strip()
                .lower()
            )
            if target_email and target_email != consumer.user.user_email.lower():
                if (
                    User.objects.exclude(user_id=consumer.user.user_id)
                    .filter(user_email__iexact=target_email)
                    .exists()
                ):
                    return Response(
                        {
                            "error": "That email address is already registered to another account."
                        },
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                consumer.user.user_email = target_email
                consumer.user.save()

            if "latitude" in request.data and "longitude" in request.data:
                try:
                    lat_v = float(request.data["latitude"])
                    lng_v = float(request.data["longitude"])
                    loc_obj = (
                        Location.objects.filter(user=consumer.user)
                        .order_by("-is_primary", "-loc_id")
                        .first()
                    )
                    if loc_obj:
                        loc_obj.address_name = consumer.address or "Cebu City"
                        loc_obj.latitude = lat_v
                        loc_obj.longitude = lng_v
                        loc_obj.save()
                    else:
                        Location.objects.create(
                            user=consumer.user,
                            address_name=consumer.address or "Cebu City",
                            latitude=lat_v,
                            longitude=lng_v,
                            is_primary=True,
                        )
                except Exception:
                    pass

            return Response(
                {
                    "message": "Account details updated successfully!",
                    "full_name": consumer.cons_fullname,
                    "email": consumer.user.user_email,
                    "phone": consumer.phone_num or "",
                },
                status=status.HTTP_200_OK,
            )

        elif action == "change_password":
            current_pw = str(request.data.get("current_password") or "").strip()
            new_pw = str(request.data.get("new_password") or "").strip()
            if len(new_pw) < 6:
                return Response(
                    {"error": "New password must be at least 6 characters."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            if current_pw and consumer.user.user_pass:
                is_valid = check_password(
                    current_pw, consumer.user.user_pass
                ) or (current_pw == consumer.user.user_pass)
                if not is_valid:
                    return Response(
                        {"error": "Your current password is incorrect."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )

            consumer.user.user_pass = make_password(new_pw)
            consumer.user.save()
            return Response(
                {"message": "Password changed successfully!"},
                status=status.HTTP_200_OK,
            )

        elif action == "deactivate_account":
            consumer.status = "Inactive"
            consumer.status_reason = "Account deactivated/deleted by user"
            consumer.save()
            consumer.user.is_banned = True
            consumer.user.save()

            AdminNotification.objects.create(
                user_id=1,
                admin_notif_type="Compliance",
                message=f"Consumer CID-{consumer.cons_id} ({consumer.cons_fullname}) deactivated their account (Status set to Inactive).",
            )
            return Response(
                {"message": "Your account has been set to Inactive."},
                status=status.HTTP_200_OK,
            )

        elif action == "submit_report":
            target_type = (
                str(request.data.get("target_type") or "listing")
                .strip()
                .lower()
            )
            prod_id = request.data.get("prod_id")
            merch_id = request.data.get("merchant_id")
            establishment_name = str(
                request.data.get("establishment") or ""
            ).strip()
            food_name = str(request.data.get("food_name") or "").strip()
            reason_category = str(
                request.data.get("reason") or "Inappropriate / Prohibited Content"
            ).strip()
            details = str(request.data.get("details") or "").strip()

            prod = None
            merch = None

            if prod_id and str(prod_id).isdigit() and int(prod_id) > 0:
                prod = (
                    PerishableProduct.objects.select_related("merchant__user")
                    .filter(prod_id=int(prod_id))
                    .first()
                )
                if prod and prod.merchant:
                    merch = prod.merchant

            if not merch and merch_id and str(merch_id).isdigit() and int(merch_id) > 0:
                merch = (
                    Merchant.objects.select_related("user")
                    .filter(merch_id=int(merch_id))
                    .first()
                )

            if not merch and establishment_name:
                merch = (
                    Merchant.objects.select_related("user")
                    .filter(bus_name__iexact=establishment_name)
                    .first()
                )

            item_label = (
                prod.prod_name
                if prod
                else (food_name or "Surplus Food Listing")
            )
            merch_label = (
                merch.bus_name
                if merch
                else (establishment_name or "Partner Merchant")
            )
            detail_summary = (
                f"{reason_category} — \"{details}\""
                if details
                else reason_category
            )

            if target_type == "merchant" and not prod:
                admin_msg = (
                    f"[Consumer Report: Storefront] {consumer.cons_fullname} (CID-{consumer.cons_id}) "
                    f"reported merchant '{merch_label}': {detail_summary}"
                )
                merch_msg = (
                    f"Consumer Report Alert ({reason_category}): Your storefront '{merch_label}' "
                    f"was reported by a consumer ({detail_summary}). Please ensure your listings and storefront "
                    f"comply with ReServe Food Safety & Community Guidelines."
                )
            else:
                fl_tag = f" (FL-{prod.prod_id})" if prod else ""
                admin_msg = (
                    f"[Consumer Report: Listing] {consumer.cons_fullname} (CID-{consumer.cons_id}) "
                    f"reported listing '{item_label}'{fl_tag} by {merch_label}: {detail_summary}"
                )
                merch_msg = (
                    f"Consumer Report Alert ({reason_category}): Your food listing '{item_label}'{fl_tag} "
                    f"was reported by a consumer ({detail_summary}). Please inspect or update your listing in "
                    f"Food Listings to maintain compliance."
                )

            admin_user = (
                User.objects.filter(user_role="Admin").first()
                or User.objects.first()
            )
            if admin_user:
                AdminNotification.objects.create(
                    user=admin_user,
                    admin_notif_type="Report",
                    message=admin_msg,
                    merchant=merch,
                )

            if merch and merch.user:
                UserNotification.objects.create(
                    user=merch.user,
                    message=merch_msg,
                )

            return Response(
                {
                    "message": (
                        f"Report submitted! ReServe Admin and {merch_label} have been notified "
                        f"for immediate compliance review."
                    )
                },
                status=status.HTTP_201_CREATED,
            )

        elif action == "submit_review":
            order_id = request.data.get("order_id")
            voucher_code = base_voucher_code(
                request.data.get("voucher_code", "")
            )
            rating_score = int(request.data.get("rating_score", 5))
            review_comment = str(request.data.get("review_comment", "")).strip()

            rating_score = max(1, min(5, rating_score))
            if voucher_code:
                batch_orders = list(
                    OrderReservation.objects.select_related(
                        "listing__listing__product__merchant__user"
                    ).filter(
                        qr_voucher_code__istartswith=voucher_code,
                        consumer=consumer,
                    )
                )
            else:
                batch_orders = list(
                    OrderReservation.objects.select_related(
                        "listing__listing__product__merchant__user"
                    ).filter(order_id=order_id, consumer=consumer)
                )

            if not batch_orders:
                return Response(
                    {"error": "Order not found."},
                    status=status.HTTP_404_NOT_FOUND,
                )
            if any(o.order_status != "Completed" for o in batch_orders):
                return Response(
                    {
                        "error": "You can only rate and review completed pickups."
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

            first_order = batch_orders[0]
            first_comm = first_order.listing
            first_prod = (
                first_comm.listing.product
                if (first_comm and first_comm.listing)
                else None
            )
            merch = first_prod.merchant if first_prod else None

            for o_obj in batch_orders:
                RatingReview.objects.update_or_create(
                    consumer=consumer,
                    order=o_obj,
                    defaults={
                        "rating_score": rating_score,
                        "review_comment": review_comment,
                        "surplus_listing": o_obj.listing,
                    },
                )

            stars_str = "★" * rating_score
            item_names = ", ".join(
                o.listing.listing.product.prod_name
                for o in batch_orders
                if o.listing and o.listing.listing and o.listing.listing.product
            )
            if merch and merch.user:
                UserNotification.objects.create(
                    user=merch.user,
                    message=f"{consumer.cons_fullname} rated '{item_names or 'your food'}' {stars_str} ({rating_score}/5): \"{review_comment or 'Great food rescue!'}\"",
                )
            AdminNotification.objects.create(
                user_id=1,
                admin_notif_type="Review",
                message=f"{consumer.cons_fullname} rated {merch.bus_name if merch else 'Merchant'} ({item_names or 'Order'}): {rating_score}/5 stars.",
                merchant=merch,
            )

            return Response(
                {
                    "message": "Thank you! Your rating and review have been posted to the food listing and merchant profile.",
                    "rating_score": rating_score,
                    "review_comment": review_comment,
                },
                status=status.HTTP_201_CREATED,
            )

        elif action == "checkout":
            items = request.data.get("items", [])
            payment_method = request.data.get("payment_method", "GCash")
            xendit_invoice_id = str(
                request.data.get("xendit_invoice_id") or ""
            ).strip()
            now = timezone.now()
            gov = _get_normalized_gov()

            if not items:
                return Response(
                    {"error": "Your cart is empty."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            is_paid, inv_status, _, err_msg = verify_xendit_invoice_paid(
                xendit_invoice_id
            )
            if not is_paid:
                return Response(
                    {
                        "error": err_msg
                        or f"Xendit payment is not yet confirmed (Status: {inv_status}). Please complete the 20% commitment fee in the Xendit checkout window first."
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

            shared_base_voucher = None
            created_claims = []
            batch_names = []
            batch_deposit_total = Decimal("0.00")
            first_merchant = None

            with transaction.atomic():
                validated_lines = []
                for cart_item in items:
                    prod_id = cart_item.get("id")
                    qty = max(1, int(cart_item.get("quantity", 1)))
                    prod = (
                        PerishableProduct.objects.filter(prod_id=prod_id)
                        .select_related("merchant__user")
                        .first()
                    )
                    if not prod:
                        continue

                    if prod.merchant:
                        is_open, open_str, close_str = (
                            is_merchant_currently_open(prod.merchant)
                        )
                        if not is_open:
                            return Response(
                                {
                                    "error": f"{prod.merchant.bus_name} is currently closed (Operating hours: {open_str} – {close_str})."
                                },
                                status=status.HTTP_400_BAD_REQUEST,
                            )

                    safety = ListingSafety.objects.filter(product=prod).first()
                    commerce = (
                        ListingCommerce.objects.select_for_update()
                        .filter(listing=safety)
                        .first()
                        if safety
                        else None
                    )
                    if not commerce or commerce.status != "Active":
                        return Response(
                            {
                                "error": f"'{prod.prod_name}' is no longer active or has been flagged/donated."
                            },
                            status=status.HTTP_400_BAD_REQUEST,
                        )
                    if safety and safety.estimated_expiry:
                        exp_dt = safety.estimated_expiry
                        if timezone.is_naive(exp_dt):
                            exp_dt = timezone.make_aware(exp_dt)
                        if (exp_dt - now).total_seconds() <= 1830:
                            commerce.status = "Flagged"
                            commerce.save(update_fields=["status"])
                            return Response(
                                {
                                    "error": f"'{prod.prod_name}' has entered the 30-minute food safety cutoff and can no longer be reserved."
                                },
                                status=status.HTTP_400_BAD_REQUEST,
                            )
                    if int(commerce.qty_available or 0) < qty:
                        return Response(
                            {
                                "error": f"Insufficient stock for {prod.prod_name} (only {commerce.qty_available if commerce else 0} left)."
                            },
                            status=status.HTTP_400_BAD_REQUEST,
                        )
                    validated_lines.append(
                        (cart_item, prod, safety, commerce, qty)
                    )

                if not validated_lines:
                    return Response(
                        {"error": "No valid listings found to reserve."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )

                first_merchant = validated_lines[0][1].merchant

                for idx, (cart_item, prod, safety, commerce, qty) in enumerate(
                    validated_lines, start=1
                ):
                    live_unit_price = Decimal(
                        str(
                            cart_item.get("price")
                            or commerce.current_price
                            or commerce.discounted_price
                            or 0
                        )
                    ).quantize(Decimal("0.01"))
                    line_subtotal = (live_unit_price * Decimal(qty)).quantize(
                        Decimal("0.01")
                    )
                    deposit_20pct = (line_subtotal * Decimal("0.20")).quantize(
                        Decimal("0.01")
                    )
                    balance_80pct = max(
                        Decimal("0.00"), line_subtotal - deposit_20pct
                    )

                    commerce.qty_available -= qty
                    commerce.qty_reserved = (commerce.qty_reserved or 0) + qty
                    commerce.current_price = live_unit_price
                    commerce.save()

                    raw_w = Decimal(str(prod.weight_per_unit or 0))
                    unit_kg = (
                        (raw_w / Decimal("1000"))
                        if (prod.unit_id == 1 and raw_w > 0)
                        else (raw_w if raw_w > 0 else Decimal("1.0"))
                    )
                    total_w_kg = (unit_kg * Decimal(qty)).quantize(
                        Decimal("0.01")
                    )

                    temp_code = f"TMP-{random.randint(100000, 999999)}-{idx}"
                    order_obj = OrderReservation.objects.create(
                        order_qty=qty,
                        order_status="Pending",
                        downpayment_amount=deposit_20pct,
                        qr_voucher_code=temp_code,
                        rescued_weight_kg=total_w_kg,
                        consumer=consumer,
                        listing=commerce,
                    )
                    if shared_base_voucher is None:
                        shared_base_voucher = f"OID{100 + order_obj.order_id}"
                        final_voucher_code = shared_base_voucher
                    else:
                        final_voucher_code = f"{shared_base_voucher}-{idx}"

                    order_obj.qr_voucher_code = final_voucher_code
                    order_obj.save()

                    is_pro = (
                        (prod.merchant.plan_id == 2) if prod.merchant else False
                    )
                    comm_rate = (
                        Decimal(str(gov.get("pro_commission_pct", 4.0)))
                        / Decimal("100")
                        if is_pro
                        else Decimal(str(gov.get("commission_pct", 10.0)))
                        / Decimal("100")
                    )
                    comm_taken = (line_subtotal * comm_rate).quantize(
                        Decimal("0.01")
                    )
                    merch_earn = (line_subtotal - comm_taken).quantize(
                        Decimal("0.01")
                    )

                    PaymentTransaction.objects.create(
                        gateway_ref_id=f"{payment_method.upper()}-{xendit_invoice_id}-{order_obj.order_id}",
                        transac_status="Success",
                        commission_taken=comm_taken,
                        merchant_earnings=merch_earn,
                        order=order_obj,
                    )

                    batch_names.append(f"{qty}x {prod.prod_name}")
                    batch_deposit_total += deposit_20pct

                    pickup_str = (
                        timezone.localtime(safety.estimated_expiry).strftime(
                            "%b %d, %Y %I:%M %p"
                        )
                        if safety and safety.estimated_expiry
                        else "Today, 5:00 PM"
                    )

                    created_claims.append(
                        {
                            "order_id": order_obj.order_id,
                            "voucher_code": shared_base_voucher,
                            "food_name": prod.prod_name,
                            "qty": qty,
                            "unit_price": f"{float(live_unit_price):.2f}",
                            "establishment": (
                                prod.merchant.bus_name
                                if prod.merchant
                                else "Partner Establishment"
                            ),
                            "address": (
                                prod.merchant.address
                                if prod.merchant and prod.merchant.address
                                else "Cebu City"
                            ),
                            "pickup_deadline": pickup_str,
                            "total_value": f"{float(line_subtotal):.2f}",
                            "paid_today": f"{float(deposit_20pct):.2f}",
                            "remaining_balance": f"{float(balance_80pct):.2f}",
                        }
                    )

            if first_merchant and first_merchant.user:
                UserNotification.objects.create(
                    user=first_merchant.user,
                    message=f"New reservation {shared_base_voucher} from {consumer.cons_fullname}: {', '.join(batch_names)} (20% Commitment Fee Paid via Xendit {payment_method}: ₱{float(batch_deposit_total):.2f}).",
                )

            AdminNotification.objects.create(
                user_id=1,
                admin_notif_type="Reservation",
                message=f"{consumer.cons_fullname} reserved {', '.join(batch_names)} ({shared_base_voucher}) from {first_merchant.bus_name if first_merchant else 'Merchant'} (20% Deposit: ₱{float(batch_deposit_total):,.2f}).",
                merchant=first_merchant,
            )

            return Response(
                {
                    "message": "Xendit 20% deposit verified and reservation confirmed!",
                    "voucher_code": shared_base_voucher,
                    "claims": created_claims,
                },
                status=status.HTTP_201_CREATED,
            )

        return Response(
            {"error": "Invalid action"}, status=status.HTTP_400_BAD_REQUEST
        )


class CreatePayMongoCheckoutView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        action = str(request.data.get("action", "create_invoice")).strip().lower()

        if action == "check_status":
            invoice_id = str(request.data.get("invoice_id") or "").strip()
            is_paid, inv_status, inv_data, err_msg = verify_xendit_invoice_paid(
                invoice_id
            )
            if err_msg and inv_status in ("MISSING", "CONFIG_ERROR", "ERROR"):
                return Response(
                    {"error": err_msg, "status": inv_status, "is_paid": False},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            return Response(
                {
                    "invoice_id": invoice_id,
                    "status": inv_status,
                    "is_paid": is_paid,
                    "amount": inv_data.get("amount"),
                    "payment_channel": inv_data.get("payment_channel"),
                },
                status=status.HTTP_200_OK,
            )

        amount_php = max(1.0, float(request.data.get("amount", 20.0)))
        description = str(
            request.data.get(
                "description", "RESERVE 20% Reservation Commitment Fee"
            )
        )
        purpose = str(request.data.get("purpose", "consumer_checkout")).lower()

        xendit_key = getattr(settings, "XENDIT_SECRET_KEY", "").strip()

        if not xendit_key or not xendit_key.startswith("xnd_"):
            return Response(
                {
                    "error": "XENDIT_SECRET_KEY is missing or invalid in .env. Please add your xnd_development_... key."
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        success_url = (
            "http://localhost:8081/establishment/profile"
            if purpose == "merchant_upgrade"
            else "http://localhost:8081/consumer/claims"
        )
        failure_url = (
            "http://localhost:8081/establishment/profile"
            if purpose == "merchant_upgrade"
            else "http://localhost:8081/consumer/cart"
        )

        try:
            res = http_requests.post(
                "https://api.xendit.co/v2/invoices",
                json={
                    "external_id": f"reserve-{uuid.uuid4().hex[:10]}",
                    "amount": round(amount_php, 2),
                    "description": description,
                    "currency": "PHP",
                    "payment_methods": ["GCASH", "PAYMAYA"],
                    "success_redirect_url": success_url,
                    "failure_redirect_url": failure_url,
                },
                auth=(xendit_key, ""),
                timeout=10,
            )
            data = res.json()
            if res.status_code in (200, 201) and data.get("invoice_url"):
                inv_id = data.get("id", "xendit-session")
                return Response(
                    {
                        "checkout_url": data["invoice_url"],
                        "invoice_id": inv_id,
                        "session_id": inv_id,
                        "amount_charged_php": amount_php,
                    },
                    status=status.HTTP_200_OK,
                )

            return Response(
                {
                    "error": data.get(
                        "message",
                        "Xendit API rejected invoice creation request.",
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        except Exception as e:
            return Response(
                {"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )