from decimal import Decimal
from django.db import transaction
from django.db.models import Avg
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.governance import get_governance_config
from accounts.models import Consumer, FoodBank, Merchant, User
from donations.models import Donation, DonationRequest
from listings.models import PerishableProduct, StockLedger
from notifications.models import AdminNotification, UserNotification
from transactions.models import OrderReservation, RatingReview


def base_voucher(raw_code):
    return str(raw_code or "").strip().upper().split("-")[0]


class MarkOrderNoShowView(APIView):
    """
    Marks an unclaimed Pending order as 'No-Show', credits the 10% deposit to the Merchant's wallet,
    and checks the Consumer's strike count against the live Admin Governance strike_limit.
    """
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        gov = get_governance_config()
        strike_limit = int(gov["strike_limit"])

        merch_id = request.data.get("merchant_id") or request.data.get("merchant")
        raw_code = base_voucher(request.data.get("order_code") or request.data.get("voucher_code"))
        force_no_show = bool(request.data.get("force", False))

        if not raw_code:
            return Response({"error": "Order voucher code is required."}, status=status.HTTP_400_BAD_REQUEST)

        merchant = None
        if merch_id and str(merch_id).isdigit():
            merchant = Merchant.objects.select_related("user").filter(merch_id=int(merch_id)).first()

        orders_qs = OrderReservation.objects.select_related(
            "consumer__user",
            "listing__listing__product__merchant__user",
        ).filter(qr_voucher_code__istartswith=raw_code)

        if merchant:
            orders_qs = orders_qs.filter(listing__listing__product__merchant=merchant)

        batch_orders = list(orders_qs)
        if not batch_orders:
            return Response({"error": f"Order {raw_code} not found."}, status=status.HTTP_404_NOT_FOUND)

        if any(o.order_status == "Completed" for o in batch_orders):
            return Response({"error": f"Order {raw_code} was already picked up and completed."}, status=status.HTTP_400_BAD_REQUEST)

        if all(o.order_status == "No-Show" for o in batch_orders):
            return Response({"error": f"Order {raw_code} is already marked as No-Show."}, status=status.HTTP_400_BAD_REQUEST)

        now = timezone.now()
        first_order = batch_orders[0]
        first_comm = first_order.listing
        first_saf = first_comm.listing if first_comm else None
        first_prod = first_saf.product if first_saf else None
        if not merchant and first_prod:
            merchant = first_prod.merchant

        if first_saf and first_saf.estimated_expiry and not force_no_show:
            exp_dt = first_saf.estimated_expiry
            if timezone.is_naive(exp_dt):
                exp_dt = timezone.make_aware(exp_dt)
            if exp_dt > now:
                exp_fmt = timezone.localtime(exp_dt).strftime("%b %d, %Y %I:%M %p")
                return Response({
                    "error": f"Cannot mark as No-Show yet. The pickup deadline ({exp_fmt}) has not passed."
                }, status=status.HTTP_400_BAD_REQUEST)

        consumer = first_order.consumer
        forfeited_deposit = Decimal("0.00")
        item_names = []

        with transaction.atomic():
            for o in batch_orders:
                if o.order_status != "Pending":
                    continue
                comm = o.listing
                prod = comm.listing.product if (comm and comm.listing) else None
                qty_val = int(o.order_qty or 1)

                if comm:
                    comm.qty_reserved = max(0, int(comm.qty_reserved or 0) - qty_val)
                    comm.save()
                    StockLedger.objects.create(
                        listing=comm,
                        transaction_type="Consumer_No_Show",
                        qty_change=-qty_val,
                        reason=f"Consumer No-Show past expiry for voucher {raw_code}",
                    )

                o.order_status = "No-Show"
                o.save()

                forfeited_deposit += Decimal(str(o.downpayment_amount or 0))
                if prod:
                    item_names.append(f"{qty_val}x {prod.prod_name}")

            if merchant and forfeited_deposit > 0:
                merchant.wallet_bal = Decimal(str(merchant.wallet_bal or 0)) + forfeited_deposit
                merchant.save()

        all_no_show_codes = set()
        if consumer:
            for code in OrderReservation.objects.filter(consumer=consumer, order_status="No-Show").values_list("qr_voucher_code", flat=True):
                all_no_show_codes.add(base_voucher(code))
        strike_count = len(all_no_show_codes)

        admin_user = User.objects.filter(user_role="Admin").first() or User.objects.first()
        items_str = ", ".join(item_names) or "Reserved Surplus Food"
        cons_label = f"{consumer.cons_fullname} (CID-{consumer.cons_id})" if consumer else "Consumer"
        merch_label = merchant.bus_name if merchant else "Merchant"

        if admin_user:
            if strike_count >= strike_limit:
                AdminNotification.objects.create(
                    user=admin_user,
                    admin_notif_type="Compliance",
                    message=(
                        f"🚨 {strike_limit}-STRIKE NO-SHOW ALERT: {cons_label} missed pickup for {raw_code} ({items_str}) at {merch_label}. "
                        f"Total No-Shows: {strike_count}/{strike_limit} — Account eligible for immediate suspension!"
                    ),
                    merchant=merchant,
                )
            else:
                AdminNotification.objects.create(
                    user=admin_user,
                    admin_notif_type="Compliance",
                    message=(
                        f"No-Show Reported (Strike {strike_count}/{strike_limit}): {cons_label} failed to claim {raw_code} ({items_str}) "
                        f"at {merch_label} before expiry."
                    ),
                    merchant=merchant,
                )

        if consumer and consumer.user:
            UserNotification.objects.create(
                user=consumer.user,
                message=(
                    f"⚠️ Missed Pickup (No-Show Strike {strike_count}/{strike_limit}): Your reservation {raw_code} ({items_str}) at {merch_label} "
                    f"expired unclaimed. Your 10% deposit (₱{float(forfeited_deposit):,.2f}) was forfeited to the store. "
                    f"Note: {strike_limit} No-Shows may result in account suspension."
                ),
            )

        if merchant and merchant.user:
            UserNotification.objects.create(
                user=merchant.user,
                message=(
                    f"No-Show recorded for {raw_code} ({items_str}). The 10% reservation deposit (₱{float(forfeited_deposit):,.2f}) "
                    f"has been credited to your Active Wallet, and Admin has been alerted (Consumer Strike {strike_count}/{strike_limit})."
                ),
            )

        return Response({
            "message": (
                f"Order {raw_code} marked as No-Show!\n"
                f"• 10% Forfeited Deposit Credited to Wallet: ₱{float(forfeited_deposit):,.2f}\n"
                f"• Consumer No-Show Strike Logged: {strike_count} / {strike_limit}\n"
                f"• ReServe Admin System has been alerted."
            ),
            "strike_count": strike_count,
            "strike_limit": strike_limit,
            "order_code": raw_code,
            "status": "No-Show",
        }, status=status.HTTP_200_OK)


class AdminUserDossierAPIView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request, role, account_id):
        role_clean = role.lower().strip()
        if role_clean == "consumer":
            return self._build_consumer_dossier(account_id)
        elif role_clean == "merchant":
            return self._build_merchant_dossier(account_id)
        elif role_clean in ("foodbank", "food_bank"):
            return self._build_foodbank_dossier(account_id)
        return Response({"error": "Invalid role"}, status=status.HTTP_400_BAD_REQUEST)

    def _group_order_receipts(self, orders_qs):
        grouped = {}
        for o in orders_qs:
            b_code = base_voucher(o.qr_voucher_code or f"ORD-{o.order_id}")
            comm = o.listing
            saf = comm.listing if comm else None
            prod = saf.product if saf else None
            merch = prod.merchant if prod else None
            cons = o.consumer

            u_price = Decimal(str(comm.discounted_price if comm else 0))
            o_price = Decimal(str(comm.orig_price if comm and comm.orig_price else u_price))
            line_tot = (u_price * Decimal(o.order_qty)).quantize(Decimal("0.01"))
            dep = Decimal(str(o.downpayment_amount or 0)).quantize(Decimal("0.01"))
            rem = max(Decimal("0.00"), line_tot - dep)

            pay_obj = getattr(o, "payment", None)
            gateway_ref = pay_obj.gateway_ref_id if pay_obj else f"XENDIT-{b_code}"

            item_entry = {
                "name": prod.prod_name if prod else f"Item #{o.order_id}",
                "category": prod.category.category_name if (prod and prod.category) else "Surplus",
                "qty": o.order_qty,
                "unit_price": f"₱{float(u_price):,.2f}",
                "orig_price": f"₱{float(o_price):,.2f}",
                "subtotal": f"₱{float(line_tot):,.2f}",
            }

            if b_code not in grouped:
                grouped[b_code] = {
                    "id": b_code,
                    "gateway_ref": gateway_ref,
                    "items": [item_entry],
                    "establishment": merch.bus_name if merch else "Partner Merchant",
                    "merchant_phone": (merch.phone or merch.hotline or "N/A") if merch else "N/A",
                    "address": merch.address if (merch and merch.address) else "Cebu City",
                    "buyer": cons.cons_fullname if cons else "Consumer",
                    "buyer_phone": cons.phone_num if (cons and cons.phone_num) else "N/A",
                    "buyer_email": cons.user.user_email if (cons and cons.user) else "",
                    "date": timezone.localtime(o.ordered_at).strftime("%b %d, %Y • %I:%M %p") if o.ordered_at else "N/A",
                    "pickup_deadline": (
                        timezone.localtime(saf.estimated_expiry).strftime("%b %d, %Y • %I:%M %p")
                        if (saf and saf.estimated_expiry)
                        else "Standard Window"
                    ),
                    "total_dec": line_tot,
                    "dep_dec": dep,
                    "rem_dec": rem,
                    "weight_kg": Decimal(str(o.rescued_weight_kg or 0)),
                    "status": o.order_status,
                }
            else:
                g = grouped[b_code]
                g["items"].append(item_entry)
                g["total_dec"] += line_tot
                g["dep_dec"] += dep
                g["rem_dec"] += rem
                g["weight_kg"] += Decimal(str(o.rescued_weight_kg or 0))

        receipts = []
        for b_code, g in grouped.items():
            it_list = g["items"]
            title_str = (
                f"{it_list[0]['name']} (x{it_list[0]['qty']})"
                if len(it_list) == 1
                else f"{it_list[0]['name']} +{len(it_list) - 1} more"
            )
            receipts.append({
                "id": b_code,
                "gateway_ref": g["gateway_ref"],
                "receipt_type": "Consumer Order Reservation",
                "title": title_str,
                "items_breakdown": it_list,
                "establishment": g["establishment"],
                "merchant_phone": g["merchant_phone"],
                "address": g["address"],
                "buyer": g["buyer"],
                "buyer_phone": g["buyer_phone"],
                "buyer_email": g["buyer_email"],
                "date": g["date"],
                "pickup_deadline": g["pickup_deadline"],
                "total_value": f"₱{float(g['total_dec']):,.2f}",
                "deposit_paid": f"₱{float(g['dep_dec']):,.2f}",
                "remaining_balance": f"₱{float(g['rem_dec']):,.2f}",
                "rescued_weight": f"{float(g['weight_kg']):,.1f} kg",
                "amount": f"₱{float(g['total_dec']):,.2f}",
                "status": g["status"],
            })
        return receipts

    def _build_consumer_dossier(self, account_id):
        gov = get_governance_config()
        strike_limit = int(gov["strike_limit"])

        consumer = Consumer.objects.select_related("user").filter(cons_id=account_id).first()
        if not consumer:
            return Response({"error": "Consumer not found"}, status=status.HTTP_404_NOT_FOUND)

        orders_qs = OrderReservation.objects.select_related(
            "consumer__user",
            "payment",
            "listing__listing__product__merchant",
            "listing__listing__product__category",
        ).filter(consumer=consumer).order_by("-ordered_at", "order_id")

        receipts = self._group_order_receipts(orders_qs)

        completed_count = sum(1 for r in receipts if r["status"] == "Completed")
        pending_count = sum(1 for r in receipts if r["status"] == "Pending")
        no_show_count = sum(1 for r in receipts if r["status"] == "No-Show")
        cancelled_count = sum(1 for r in receipts if r["status"] == "Cancelled")

        reviews_qs = RatingReview.objects.select_related(
            "surplus_listing__listing__product__merchant",
            "order",
        ).filter(consumer=consumer).order_by("-review_id")

        reviews_given = []
        for rev in reviews_qs:
            prod = (
                rev.surplus_listing.listing.product
                if (rev.surplus_listing and rev.surplus_listing.listing)
                else None
            )
            reviews_given.append({
                "id": rev.review_id,
                "voucher": base_voucher(rev.order.qr_voucher_code) if rev.order else "Verified Order",
                "item": prod.prod_name if prod else "Surplus Food",
                "merchant": prod.merchant.bus_name if (prod and prod.merchant) else "Partner Merchant",
                "score": rev.rating_score,
                "comment": rev.review_comment or "No written comment provided.",
                "date": timezone.localtime(rev.created_at).strftime("%b %d, %Y • %I:%M %p") if getattr(rev, "created_at", None) else "Recent",
            })

        avg_given = reviews_qs.aggregate(avg=Avg("rating_score"))["avg"] or 0.0

        email_str = consumer.user.user_email if consumer.user else ""
        cid_tag = f"CID-{consumer.cons_id}"

        reports_received = []
        for r in receipts:
            if r["status"] == "No-Show":
                reports_received.append({
                    "id": f"NOSHOW-{r['id']}",
                    "type": "No-Show Strike",
                    "severity": "High" if no_show_count >= strike_limit else "Warning",
                    "message": f"Missed pickup deadline ({r['pickup_deadline']}) for Voucher {r['id']} ({r['title']}) at {r['establishment']}.",
                    "date": r["date"],
                })

        comp_alerts = AdminNotification.objects.filter(
            admin_notif_type="Compliance",
            message__icontains=cid_tag,
        ).order_by("-pk")[:15]
        for ca in comp_alerts:
            reports_received.append({
                "id": f"COMP-{ca.pk}",
                "type": "Admin Compliance Log",
                "severity": "High",
                "message": ca.message,
                "date": timezone.localtime(ca.created_at).strftime("%b %d, %Y • %I:%M %p") if getattr(ca, "created_at", None) else "Logged",
            })

        issues_reported = []
        if email_str:
            sup_notifs = AdminNotification.objects.filter(
                admin_notif_type="Support",
                message__icontains=email_str,
            ).order_by("-pk")[:15]
            for sn in sup_notifs:
                issues_reported.append({
                    "id": f"SUP-{sn.pk}",
                    "type": "Support Ticket / Report",
                    "message": sn.message,
                    "date": timezone.localtime(sn.created_at).strftime("%b %d, %Y • %I:%M %p") if getattr(sn, "created_at", None) else "Submitted",
                })

        return Response({
            "role": "Consumer",
            "account_id": consumer.cons_id,
            "status": consumer.status or "Active",
            "status_reason": consumer.status_reason or "",
            "behavior": {
                "no_show_strikes": no_show_count,
                "strike_limit": strike_limit,
                "suspension_recommended": no_show_count >= strike_limit,
                "total_orders": len(receipts),
                "completed_orders": completed_count,
                "pending_orders": pending_count,
                "cancelled_orders": cancelled_count,
                "total_food_saved_kg": f"{float(consumer.total_food_saved or 0):,.1f} kg",
                "avg_rating_given": round(float(avg_given), 1) if reviews_given else 0.0,
                "reviews_count": len(reviews_given),
                "reports_received_count": len(reports_received),
                "issues_reported_count": len(issues_reported),
            },
            "transactions": receipts,
            "reviews": reviews_given,
            "reports_received": reports_received,
            "issues_reported": issues_reported,
        }, status=status.HTTP_200_OK)

    def _build_merchant_dossier(self, account_id):
        merchant = Merchant.objects.select_related("user", "plan").filter(merch_id=account_id).first()
        if not merchant:
            return Response({"error": "Merchant not found"}, status=status.HTTP_404_NOT_FOUND)

        orders_qs = OrderReservation.objects.select_related(
            "consumer__user",
            "payment",
            "listing__listing__product__merchant",
            "listing__listing__product__category",
        ).filter(listing__listing__product__merchant=merchant).order_by("-ordered_at", "order_id")

        order_receipts = self._group_order_receipts(orders_qs)

        donations_qs = Donation.objects.select_related(
            "food_bank__user",
            "listing__listing__product__unit",
        ).filter(merchant=merchant).order_by("-donation_id")

        donation_receipts = []
        for d in donations_qs:
            prod = d.listing.listing.product if (d.listing and d.listing.listing) else None
            u_name = prod.unit.unit_name if (prod and prod.unit) else "units"
            p_name = prod.prod_name if prod else "Surplus Donation"
            donation_receipts.append({
                "id": f"DON-{d.donation_id}",
                "gateway_ref": f"HANDSHAKE-DON-{d.donation_id}",
                "receipt_type": "Food Bank Donation Handshake",
                "title": f"{p_name} (x{d.qty_donated})",
                "items_breakdown": [{
                    "name": p_name,
                    "category": "Charitable Donation",
                    "qty": d.qty_donated,
                    "unit_price": "₱0.00 (Donated)",
                    "orig_price": "Surplus Rescue",
                    "subtotal": f"{d.qty_donated} {u_name}",
                }],
                "establishment": merchant.bus_name,
                "merchant_phone": merchant.phone or merchant.hotline or "N/A",
                "address": merchant.address or "Cebu City",
                "buyer": d.food_bank.org_name if d.food_bank else "Open to Food Banks",
                "buyer_phone": d.food_bank.phone if (d.food_bank and d.food_bank.phone) else "N/A",
                "buyer_email": d.food_bank.user.user_email if (d.food_bank and d.food_bank.user) else "",
                "date": timezone.localtime(d.created_at).strftime("%b %d, %Y • %I:%M %p") if d.created_at else "Recent",
                "pickup_deadline": "Direct NGO Handshake",
                "total_value": f"{d.qty_donated} {u_name} (Donated)",
                "deposit_paid": "₱0.00 (Zero-Cost)",
                "remaining_balance": "₱0.00",
                "rescued_weight": f"{d.qty_donated} kg",
                "amount": f"{d.qty_donated} {u_name}",
                "status": d.status,
            })

        all_transactions = order_receipts + donation_receipts

        reviews_qs = RatingReview.objects.select_related(
            "consumer",
            "surplus_listing__listing__product",
            "order",
        ).filter(surplus_listing__listing__product__merchant=merchant).order_by("-review_id")

        reviews_received = []
        for rev in reviews_qs:
            prod = (
                rev.surplus_listing.listing.product
                if (rev.surplus_listing and rev.surplus_listing.listing)
                else None
            )
            reviews_received.append({
                "id": rev.review_id,
                "voucher": base_voucher(rev.order.qr_voucher_code) if rev.order else "Verified Order",
                "item": prod.prod_name if prod else "Surplus Food",
                "consumer": rev.consumer.cons_fullname if rev.consumer else "Verified Consumer",
                "score": rev.rating_score,
                "comment": rev.review_comment or "No written comment provided.",
                "date": timezone.localtime(rev.created_at).strftime("%b %d, %Y • %I:%M %p") if getattr(rev, "created_at", None) else "Recent",
            })

        avg_score = reviews_qs.aggregate(avg=Avg("rating_score"))["avg"] or 0.0

        email_str = merchant.user.user_email if merchant.user else ""
        reports_received = []
        for an in AdminNotification.objects.filter(merchant=merchant, admin_notif_type="Compliance").order_by("-pk")[:15]:
            reports_received.append({
                "id": f"COMP-{an.pk}",
                "type": "Compliance / Flag Notice",
                "severity": "Warning",
                "message": an.message,
                "date": timezone.localtime(an.created_at).strftime("%b %d, %Y • %I:%M %p") if getattr(an, "created_at", None) else "Logged",
            })

        issues_reported = []
        if email_str:
            for sn in AdminNotification.objects.filter(admin_notif_type="Support", message__icontains=email_str).order_by("-pk")[:15]:
                issues_reported.append({
                    "id": f"SUP-{sn.pk}",
                    "type": "Merchant Support Report",
                    "message": sn.message,
                    "date": timezone.localtime(sn.created_at).strftime("%b %d, %Y • %I:%M %p") if getattr(sn, "created_at", None) else "Submitted",
                })

        active_listings_count = PerishableProduct.objects.filter(
            merchant=merchant,
            safety_profiles__commerce__status="Active",
        ).distinct().count()

        return Response({
            "role": "Merchant",
            "account_id": merchant.merch_id,
            "status": merchant.status or "Active",
            "behavior": {
                "avg_rating": round(float(avg_score), 1) if reviews_received else 0.0,
                "reviews_count": len(reviews_received),
                "active_listings": active_listings_count,
                "completed_sales": sum(1 for r in order_receipts if r["status"] == "Completed"),
                "no_shows_experienced": sum(1 for r in order_receipts if r["status"] == "No-Show"),
                "donations_posted": len(donation_receipts),
                "wallet_balance": f"₱{float(merchant.wallet_bal or 0):,.2f}",
                "plan_name": merchant.plan.plan_name if merchant.plan else "Basic",
                "reports_received_count": len(reports_received),
                "issues_reported_count": len(issues_reported),
            },
            "transactions": all_transactions,
            "reviews": reviews_received,
            "reports_received": reports_received,
            "issues_reported": issues_reported,
        }, status=status.HTTP_200_OK)

    def _build_foodbank_dossier(self, account_id):
        fb = FoodBank.objects.select_related("user").filter(org_id=account_id).first()
        if not fb:
            return Response({"error": "Food Bank not found"}, status=status.HTTP_404_NOT_FOUND)

        appeals_qs = DonationRequest.objects.select_related(
            "merchant",
            "listing__listing__product__unit",
        ).filter(food_bank=fb).order_by("-donation_req_id")

        donations_qs = Donation.objects.select_related(
            "merchant",
            "listing__listing__product__unit",
        ).filter(food_bank=fb).order_by("-donation_id")

        handshakes = []
        grouped_appeals = {}
        for req in appeals_qs:
            b_code = req.batch_code or f"REQ-{req.donation_req_id}"
            prod = req.listing.listing.product if (req.listing and req.listing.listing) else None
            u_name = prod.unit.unit_name if (prod and prod.unit) else "items"
            item_entry = {
                "name": prod.prod_name if prod else req.food_type,
                "category": req.food_type,
                "qty": req.qty_needed,
                "unit_price": "₱0.00 (Appeal)",
                "orig_price": "Food Bank Appeal",
                "subtotal": f"{req.qty_needed} {u_name}",
            }
            if b_code not in grouped_appeals:
                grouped_appeals[b_code] = {
                    "id": b_code,
                    "gateway_ref": f"APPEAL-{b_code}",
                    "receipt_type": "Food Bank Bulk / Single Appeal",
                    "items": [item_entry],
                    "establishment": req.merchant.bus_name if req.merchant else "Partner Merchant",
                    "merchant_phone": (req.merchant.phone or req.merchant.hotline or "N/A") if req.merchant else "N/A",
                    "address": (req.merchant.address or "Cebu City") if req.merchant else "Cebu City",
                    "buyer": fb.org_name,
                    "buyer_phone": fb.phone or fb.hotline or "N/A",
                    "buyer_email": fb.user.user_email if fb.user else "",
                    "date": timezone.localtime(req.created_at).strftime("%b %d, %Y • %I:%M %p") if req.created_at else "Recent",
                    "pickup_deadline": req.reason or "Community distribution",
                    "total_qty": int(req.qty_needed or 0),
                    "unit_name": u_name,
                    "status": req.status,
                }
            else:
                g = grouped_appeals[b_code]
                g["items"].append(item_entry)
                g["total_qty"] += int(req.qty_needed or 0)

        for b_code, g in grouped_appeals.items():
            it_list = g["items"]
            title_s = (
                f"{it_list[0]['name']} (x{it_list[0]['qty']})"
                if len(it_list) == 1
                else f"{it_list[0]['name']} +{len(it_list) - 1} more"
            )
            handshakes.append({
                "id": b_code,
                "gateway_ref": g["gateway_ref"],
                "receipt_type": g["receipt_type"],
                "title": title_s,
                "items_breakdown": it_list,
                "establishment": g["establishment"],
                "merchant_phone": g["merchant_phone"],
                "address": g["address"],
                "buyer": g["buyer"],
                "buyer_phone": g["buyer_phone"],
                "buyer_email": g["buyer_email"],
                "date": g["date"],
                "pickup_deadline": f"Reason: {g['pickup_deadline']}",
                "total_value": f"{g['total_qty']} {g['unit_name']} (Requested)",
                "deposit_paid": "₱0.00 (NGO Partner)",
                "remaining_balance": "₱0.00",
                "amount": f"{g['total_qty']} {g['unit_name']}",
                "status": g["status"],
            })

        for d in donations_qs:
            prod = d.listing.listing.product if (d.listing and d.listing.listing) else None
            u_name = prod.unit.unit_name if (prod and prod.unit) else "units"
            p_name = prod.prod_name if prod else "Surplus Donation"
            handshakes.append({
                "id": f"DON-{d.donation_id}",
                "gateway_ref": f"CLAIM-DON-{d.donation_id}",
                "receipt_type": "Claimed Surplus Donation",
                "title": f"{p_name} (x{d.qty_donated})",
                "items_breakdown": [{
                    "name": p_name,
                    "category": "Surplus Donation",
                    "qty": d.qty_donated,
                    "unit_price": "₱0.00 (Donated)",
                    "orig_price": "Merchant Posted",
                    "subtotal": f"{d.qty_donated} {u_name}",
                }],
                "establishment": d.merchant.bus_name if d.merchant else "Partner Merchant",
                "merchant_phone": (d.merchant.phone or d.merchant.hotline or "N/A") if d.merchant else "N/A",
                "address": (d.merchant.address or "Cebu City") if d.merchant else "Cebu City",
                "buyer": fb.org_name,
                "buyer_phone": fb.phone or fb.hotline or "N/A",
                "buyer_email": fb.user.user_email if fb.user else "",
                "date": timezone.localtime(d.created_at).strftime("%b %d, %Y • %I:%M %p") if d.created_at else "Recent",
                "pickup_deadline": "Off-App Handshake",
                "total_value": f"{d.qty_donated} {u_name} (Claimed)",
                "deposit_paid": "₱0.00",
                "remaining_balance": "₱0.00",
                "amount": f"{d.qty_donated} {u_name}",
                "status": d.status,
            })

        email_str = fb.user.user_email if fb.user else ""
        fb_tag = f"FB-{fb.org_id}"

        reports_received = []
        for ca in AdminNotification.objects.filter(admin_notif_type="Compliance", message__icontains=fb_tag).order_by("-pk")[:15]:
            reports_received.append({
                "id": f"COMP-{ca.pk}",
                "type": "Compliance Log",
                "severity": "Warning",
                "message": ca.message,
                "date": timezone.localtime(ca.created_at).strftime("%b %d, %Y • %I:%M %p") if getattr(ca, "created_at", None) else "Logged",
            })

        issues_reported = []
        if email_str:
            for sn in AdminNotification.objects.filter(admin_notif_type="Support", message__icontains=email_str).order_by("-pk")[:15]:
                issues_reported.append({
                    "id": f"SUP-{sn.pk}",
                    "type": "Food Bank Support Ticket",
                    "message": sn.message,
                    "date": timezone.localtime(sn.created_at).strftime("%b %d, %Y • %I:%M %p") if getattr(sn, "created_at", None) else "Submitted",
                })

        return Response({
            "role": "FoodBank",
            "account_id": fb.org_id,
            "status": fb.status or "Active",
            "behavior": {
                "total_rescued_units": f"{float(fb.total_food_received or 0):,.1f} Units",
                "total_appeals": len(grouped_appeals),
                "successful_handshakes": sum(
                    1 for h in handshakes if h["status"] in ("Successful", "Fulfilled", "Completed")
                ),
                "ongoing_handshakes": sum(
                    1 for h in handshakes if h["status"] in ("Ongoing", "Claimed", "Pending", "Active")
                ),
                "withdrawn_or_rejected": sum(
                    1 for h in handshakes if h["status"] in ("Withdrawn", "Rejected", "Cancelled")
                ),
                "reports_received_count": len(reports_received),
                "issues_reported_count": len(issues_reported),
            },
            "transactions": handshakes,
            "reviews": [],
            "reports_received": reports_received,
            "issues_reported": issues_reported,
        }, status=status.HTTP_200_OK)