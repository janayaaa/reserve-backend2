import json
import datetime
import uuid
import random
import re
import math
import inspect
import requests
from decimal import Decimal
from django.shortcuts import render, redirect
from django.db.models import Sum, Count, Avg, Q
from django.apps import apps
from django.contrib.auth.hashers import check_password, make_password
from django.core.mail import send_mail
from django.conf import settings
from django.core.files.storage import default_storage
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.utils import timezone
from django.core.cache import cache
from django.db import connection, transaction
from django.contrib.auth import logout as django_logout
from django.views.decorators.cache import never_cache

from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.parsers import JSONParser, MultiPartParser, FormParser
from rest_framework_simplejwt.views import TokenObtainPairView

from notifications.models import AdminNotification, UserNotification, Announcement
from transactions.models import WithdrawalReq, RatingReview, OrderReservation, PaymentTransaction

from .models import Consumer, Merchant, User, FoodBank, Location, UserSession
from .serializers import UserRegistrationSerializer, CustomLoginSerializer
from .merchant_serializers import MerchantRegistrationSerializer
from .utils import extract_cebu_permit_data, build_cebu_permit_eligibility_report
from .governance import get_governance_config, save_governance_config

OTP_MEMORY_STORE = {}
PASSWORD_RESET_STORE = {}


def send_compliance_email(to_email, subject, body):
    print(f"\n--- [EMAIL DISPATCH TO: {to_email}] ---\nSubject: {subject}\n{body}\n--------------------------------------\n")
    try:
        send_mail(
            subject=subject,
            message=body,
            from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', 'reserve.org.com@gmail.com'),
            recipient_list=[to_email],
            fail_silently=False,
        )
        return True
    except Exception as e:
        print(f"SMTP Email Warning: {e}")
        return False


def send_twilio_sms(to_e164_phone, message_body):
    textbee_key = getattr(settings, 'TEXTBEE_API_KEY', '').strip()
    textbee_device = getattr(settings, 'TEXTBEE_DEVICE_ID', '').strip()

    print(f"\n--- [TEXTBEE SMS DISPATCH TO: {to_e164_phone}] ---\n{message_body}\n--------------------------------------------\n")

    if not textbee_key or not textbee_device:
        return False

    try:
        url = f"https://api.textbee.dev/api/v1/gateway/devices/{textbee_device}/send-sms"
        res = requests.post(
            url,
            json={"recipients": [to_e164_phone], "message": message_body},
            headers={"x-api-key": textbee_key},
            timeout=10,
        )
        return res.status_code in (200, 201)
    except Exception as e:
        print("TextBee SMS Error:", e)
        return False


def normalize_ph_phone(phone_raw):
    if not phone_raw:
        return None, None

    raw_str = str(phone_raw).strip()
    digits = re.sub(r'\D', '', raw_str)

    if digits.startswith('6309') and len(digits) == 13:
        digits = digits[2:]
    elif digits.startswith('639') and len(digits) == 12:
        digits = '0' + digits[2:]
    elif digits.startswith('9') and len(digits) == 10:
        digits = '0' + digits

    if len(digits) == 11 and digits.startswith('09'):
        subscriber = digits[1:]
        return f"+63{subscriber}", f"0{subscriber}"

    return None, None


def is_phone_already_registered(phone_raw, exclude_user_id=None):
    e164_phone, local_phone = normalize_ph_phone(phone_raw)
    raw_clean = str(phone_raw or '').strip()
    candidates = {c for c in [e164_phone, local_phone, raw_clean] if c}
    if not candidates:
        return False

    q_cons, q_merch, q_fb = Q(), Q(), Q()
    for c in candidates:
        q_cons |= Q(phone_num__iexact=c)
        q_merch |= Q(phone__iexact=c)
        q_fb |= Q(phone__iexact=c)

    cons_qs = Consumer.objects.filter(q_cons)
    merch_qs = Merchant.objects.filter(q_merch)
    fb_qs = FoodBank.objects.filter(q_fb)

    if exclude_user_id:
        cons_qs = cons_qs.exclude(user_id=exclude_user_id)
        merch_qs = merch_qs.exclude(user_id=exclude_user_id)
        fb_qs = fb_qs.exclude(user_id=exclude_user_id)

    return cons_qs.exists() or merch_qs.exists() or fb_qs.exists()


def _haversine_distance_km(lat1, lon1, lat2, lon2):
    try:
        r = 6371.0
        d_lat = math.radians(float(lat2) - float(lat1))
        d_lon = math.radians(float(lon2) - float(lon1))
        a = (
            math.sin(d_lat / 2) ** 2
            + math.cos(math.radians(float(lat1)))
            * math.cos(math.radians(float(lat2)))
            * math.sin(d_lon / 2) ** 2
        )
        return r * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    except Exception:
        return 999.0


def find_duplicate_pending_merchant(bus_name, address, latitude=None, longitude=None):
    clean_name = re.sub(r'\s+', ' ', str(bus_name or '')).strip()
    if not clean_name or clean_name == 'Pending OCR Permit Extraction':
        return None

    clean_addr = re.sub(r'\s+', ' ', str(address or '')).strip().lower()
    pending_qs = Merchant.objects.select_related('user').filter(
        status__in=['Pending', 'Resubmit Required'],
        bus_name__iexact=clean_name,
    )

    for m in pending_qs:
        existing_addr = re.sub(r'\s+', ' ', str(m.address or '')).strip().lower()
        if clean_addr and existing_addr:
            if clean_addr == existing_addr or clean_addr in existing_addr or existing_addr in clean_addr:
                return m

        if latitude is not None and longitude is not None:
            loc = Location.objects.filter(user=m.user).order_by('-is_primary', '-loc_id').first()
            if loc and _haversine_distance_km(latitude, longitude, loc.latitude, loc.longitude) <= 0.25:
                return m

    return None


def log_system_activity(notif_type, message, target_user=None, merchant=None, withdrawal=None):
    try:
        admin_user = User.objects.filter(user_role='Admin').first() or User.objects.first()
        if admin_user:
            AdminNotification.objects.create(
                user=admin_user,
                admin_notif_type=notif_type[:100],
                message=message,
                merchant=merchant,
                withdrawal=withdrawal,
            )
        if target_user:
            UserNotification.objects.create(user=target_user, message=message)
    except Exception as e:
        print("Activity log notice:", e)


def resolve_admin_order_live_pricing(order_obj, tx_map=None):
    """
    Synchronizes Admin order tables and digital receipts with the exact live dynamic
    price locked in at checkout (from PaymentTransaction + OrderReservation.downpayment_amount).
    Returns (unit_price, orig_unit_price, line_total, orig_line_total, deposit_paid, remaining_balance, gateway_ref).
    """
    qty = max(1, int(order_obj.order_qty or 1))
    commerce = order_obj.listing
    fallback_disc = Decimal(
        str((commerce.current_price or commerce.discounted_price) if commerce else 0)
    ).quantize(Decimal("0.01"))
    orig_unit = Decimal(
        str(commerce.orig_price if (commerce and commerce.orig_price) else fallback_disc)
    ).quantize(Decimal("0.01"))
    orig_line_total = (orig_unit * Decimal(qty)).quantize(Decimal("0.01"))

    dep_raw = Decimal(str(order_obj.downpayment_amount or 0)).quantize(Decimal("0.01"))

    tx = None
    if tx_map is not None:
        tx = tx_map.get(order_obj.order_id)
    else:
        tx = PaymentTransaction.objects.filter(order_id=order_obj.order_id).first()

    tx_gross = Decimal("0.00")
    gateway_ref = ""
    if tx:
        tx_gross = (
            Decimal(str(tx.commission_taken or 0))
            + Decimal(str(tx.merchant_earnings or 0))
        ).quantize(Decimal("0.01"))
        gateway_ref = str(tx.gateway_ref_id or "")

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
        gateway_ref,
    )


def disburse_xendit_payout_accounts(channel, account_number, account_name, net_amount_dec, external_id):
    xendit_key = getattr(settings, "XENDIT_SECRET_KEY", "").strip()
    is_gcash = "MAYA" not in str(channel or "GCash").upper()
    v2_channel_code = "PH_GCASH" if is_gcash else "PH_PAYMAYA"
    v1_bank_code = "GCASH" if is_gcash else "PAYMAYA"
    fallback_ref = f"XND-{str(channel).upper()}-{uuid.uuid4().hex[:8].upper()}"

    _, local_phone = normalize_ph_phone(account_number)
    clean_acct_number = local_phone or re.sub(r"\D", "", str(account_number or "")).strip() or str(account_number or "").strip()
    clean_holder_name = str(account_name or "Merchant Partner").strip()
    payout_amount_float = round(float(net_amount_dec), 2)
    payout_amount_int = max(1, int(round(float(net_amount_dec))))

    if not xendit_key or not xendit_key.startswith("xnd_"):
        return fallback_ref

    try:
        res_v2 = requests.post(
            "https://api.xendit.co/v2/payouts",
            json={
                "reference_id": external_id,
                "channel_code": v2_channel_code,
                "channel_properties": {
                    "account_holder_name": clean_holder_name,
                    "account_number": clean_acct_number,
                },
                "amount": payout_amount_float,
                "description": f"RESERVE Automated Merchant Payout ({channel})",
                "currency": "PHP",
            },
            headers={"Idempotency-Key": external_id, "Content-Type": "application/json"},
            auth=(xendit_key, ""),
            timeout=12,
        )
        data_v2 = res_v2.json() if res_v2.content else {}
        if res_v2.status_code in (200, 201) and isinstance(data_v2, dict) and data_v2.get("id"):
            return str(data_v2["id"])
    except Exception as e_v2:
        print("Xendit v2 Payout notice:", e_v2)

    try:
        res_v1 = requests.post(
            "https://api.xendit.co/disbursements",
            json={
                "external_id": external_id,
                "bank_code": v1_bank_code,
                "account_holder_name": clean_holder_name,
                "account_number": clean_acct_number,
                "description": f"RESERVE Automated Merchant Payout ({channel})",
                "amount": payout_amount_int,
            },
            auth=(xendit_key, ""),
            timeout=12,
        )
        data_v1 = res_v1.json() if res_v1.content else {}
        if res_v1.status_code in (200, 201) and isinstance(data_v1, dict) and data_v1.get("id"):
            return f"XND-{str(data_v1['id'])[-10:].upper()}"
    except Exception as e_v1:
        print("Xendit v1 Disbursement notice:", e_v1)

    return fallback_ref


def evaluate_permit_eligibility(ocr_results, expected_bus_name="", expected_address=""):
    if not isinstance(ocr_results, dict):
        ocr_results = {}
    if isinstance(ocr_results.get("eligibility_report"), dict):
        return ocr_results["eligibility_report"]
    return build_cebu_permit_eligibility_report(
        ocr_results,
        expected_bus_name=expected_bus_name,
        expected_address=expected_address,
    )


class SendRegistrationOTPView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        channel = str(request.data.get('channel', '')).strip().lower()
        email = str(request.data.get('email', '')).strip().lower()
        raw_phone = str(request.data.get('phone') or request.data.get('phone_number') or request.data.get('phoneNumber') or '').strip()

        bus_name = str(request.data.get('bus_name') or '').strip()
        address = str(request.data.get('address') or '').strip()
        lat = request.data.get('latitude')
        lng = request.data.get('longitude')

        if bus_name:
            dup_merchant = find_duplicate_pending_merchant(bus_name, address, lat, lng)
            if dup_merchant:
                return Response({
                    "error": f"An application for '{dup_merchant.bus_name}' at this location has already been submitted and is currently awaiting Admin approval."
                }, status=status.HTTP_400_BAD_REQUEST)

        if raw_phone and is_phone_already_registered(raw_phone):
            return Response({
                "error": "This mobile phone number is already registered to an existing RESERVE account. Please use a different number or log in."
            }, status=status.HTTP_400_BAD_REQUEST)

        if channel == 'sms' or (raw_phone and not email):
            e164_phone, local_phone = normalize_ph_phone(raw_phone)
            if not e164_phone:
                return Response({
                    "error": f"Invalid Philippine phone number ('{raw_phone}'). Please enter an 11-digit mobile number starting with 09."
                }, status=status.HTTP_400_BAD_REQUEST)

            sms_otp = f"{random.randint(100000, 999999)}"
            sms_key = f"sms_{e164_phone}"
            OTP_MEMORY_STORE[sms_key] = {"sms_otp": sms_otp, "phone": e164_phone, "verified": False}
            try:
                cache.set(f"reserve_{sms_key}", OTP_MEMORY_STORE[sms_key], timeout=600)
            except Exception:
                pass

            sms_sent = send_twilio_sms(e164_phone, f"RESERVE Cebu: Your 6-digit phone verification code is {sms_otp}. Valid for 10 mins.")
            return Response({
                "message": f"6-digit SMS OTP sent to {e164_phone}.",
                "normalized_phone": e164_phone,
                "local_phone": local_phone,
                "sms_sent": sms_sent,
            }, status=status.HTTP_200_OK)

        if not email or '@' not in email:
            return Response({"error": "Please enter a valid email address."}, status=status.HTTP_400_BAD_REQUEST)

        if User.objects.filter(user_email__iexact=email).exists():
            return Response({"error": "This email address is already registered."}, status=status.HTTP_400_BAD_REQUEST)

        email_otp = f"{random.randint(100000, 999999)}"
        OTP_MEMORY_STORE[email] = {"email_otp": email_otp, "phone": raw_phone}
        try:
            cache.set(f"reserve_otp_{email}", OTP_MEMORY_STORE[email], timeout=600)
        except Exception:
            pass

        email_sent = send_compliance_email(
            email,
            "RESERVE Cebu - Email Verification Code",
            f"Welcome to RESERVE!\n\nYour 6-digit Email Verification Code is: {email_otp}\n\nThis code expires in 10 minutes."
        )
        return Response({"message": f"Email verification code sent to {email}.", "email_sent": email_sent}, status=status.HTTP_200_OK)


class VerifyRegistrationOTPView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        channel = str(request.data.get('channel', '')).strip().lower()
        email = str(request.data.get('email', '')).strip().lower()
        raw_phone = str(request.data.get('phone') or request.data.get('phone_number') or request.data.get('phoneNumber') or '').strip()
        email_code = str(request.data.get('otp', '')).strip()
        sms_code = str(request.data.get('sms_otp', '')).strip()

        if channel == 'sms' or (sms_code and not email_code):
            e164_phone, _ = normalize_ph_phone(raw_phone)
            if not e164_phone:
                return Response({"error": "Invalid Philippine phone number."}, status=status.HTTP_400_BAD_REQUEST)

            sms_key = f"sms_{e164_phone}"
            record = OTP_MEMORY_STORE.get(sms_key) or cache.get(f"reserve_{sms_key}")
            if not record:
                return Response({"error": "SMS OTP expired or not found. Please tap 'Resend OTP'."}, status=status.HTTP_400_BAD_REQUEST)

            if sms_code != str(record.get("sms_otp", "")):
                return Response({"error": "Invalid 6-digit SMS OTP code. Please check your text messages."}, status=status.HTTP_400_BAD_REQUEST)

            record["verified"] = True
            OTP_MEMORY_STORE[sms_key] = record
            try:
                cache.set(f"reserve_{sms_key}", record, timeout=600)
            except Exception:
                pass
            return Response({"message": "Phone number verified successfully!"}, status=status.HTTP_200_OK)

        if raw_phone:
            e164_phone, _ = normalize_ph_phone(raw_phone)
            sms_key = f"sms_{e164_phone}" if e164_phone else ""
            sms_record = OTP_MEMORY_STORE.get(sms_key) or (cache.get(f"reserve_{sms_key}") if sms_key else None)
            if not sms_record or not sms_record.get("verified"):
                return Response({"error": "Please verify your Philippine mobile number via SMS OTP first."}, status=status.HTTP_400_BAD_REQUEST)

        record = OTP_MEMORY_STORE.get(email) or cache.get(f"reserve_otp_{email}")
        if not record:
            return Response({"error": "Email verification session expired. Please request a new code."}, status=status.HTTP_400_BAD_REQUEST)

        expected_email = str(record.get("email_otp", "")) if isinstance(record, dict) else str(record)
        if email_code != expected_email:
            return Response({"error": "Invalid Email OTP code. Please check your inbox."}, status=status.HTTP_400_BAD_REQUEST)

        OTP_MEMORY_STORE.pop(email, None)
        try:
            cache.delete(f"reserve_otp_{email}")
        except Exception:
            pass
        return Response({"message": "Both Phone and Email verified!"}, status=status.HTTP_200_OK)


class ForgotPasswordRequestView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        is_admin_portal = bool(request.data.get('is_admin', False))
        email = str(request.data.get('email', '')).strip().lower()

        if is_admin_portal:
            target_user = User.objects.filter(user_role='Admin').first()
            recipient_email = getattr(settings, 'EMAIL_HOST_USER', 'reserve.org.com@gmail.com')
            if not target_user:
                return Response({"error": "No Admin account found in database."}, status=status.HTTP_404_NOT_FOUND)
            account_email = target_user.user_email
        else:
            if not email or '@' not in email:
                return Response({"error": "Please enter your registered email address."}, status=status.HTTP_400_BAD_REQUEST)
            target_user = User.objects.filter(user_email__iexact=email).first()
            if not target_user:
                return Response({"error": "No RESERVE account found with that email address."}, status=status.HTTP_404_NOT_FOUND)
            recipient_email = target_user.user_email
            account_email = target_user.user_email

        token = uuid.uuid4().hex
        PASSWORD_RESET_STORE[token] = {"user_id": target_user.user_id, "email": account_email, "is_admin": is_admin_portal}
        try:
            cache.set(f"reserve_pw_reset_{token}", PASSWORD_RESET_STORE[token], timeout=1800)
        except Exception:
            pass

        host_scheme = request.scheme or "http"
        host_domain = request.get_host() or "127.0.0.1:8000"
        reset_link = f"{host_scheme}://{host_domain}/reset-password/?token={token}&email={account_email}"

        send_compliance_email(
            recipient_email,
            "RESERVE Cebu - Password Reset Link",
            f"Hello,\n\nA password reset was requested for your RESERVE account ({account_email}).\n\n"
            f"Click the link below to set a new password:\n{reset_link}\n\n"
            f"This link is valid for 30 minutes."
        )
        return Response({
            "message": f"Password reset link has been sent to {recipient_email}. Please check your inbox."
        }, status=status.HTTP_200_OK)


class ResetPasswordConfirmView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        token = str(request.data.get('token', '')).strip()
        new_password = str(request.data.get('new_password', '')).strip()
        confirm_password = str(request.data.get('confirm_password', '')).strip()

        if not token:
            return Response({"error": "Invalid or missing reset token."}, status=status.HTTP_400_BAD_REQUEST)
        if len(new_password) < 6:
            return Response({"error": "Password must be at least 6 characters long."}, status=status.HTTP_400_BAD_REQUEST)
        if new_password != confirm_password:
            return Response({"error": "Passwords do not match."}, status=status.HTTP_400_BAD_REQUEST)

        record = PASSWORD_RESET_STORE.get(token) or cache.get(f"reserve_pw_reset_{token}")
        if not record:
            return Response({"error": "This password reset link has expired or already been used."}, status=status.HTTP_400_BAD_REQUEST)

        user = User.objects.filter(user_id=record['user_id']).first()
        if not user:
            return Response({"error": "Account not found."}, status=status.HTTP_404_NOT_FOUND)

        user.user_pass = make_password(new_password)
        user.save()

        PASSWORD_RESET_STORE.pop(token, None)
        try:
            cache.delete(f"reserve_pw_reset_{token}")
        except Exception:
            pass

        log_system_activity("Security", f"Password successfully reset for {user.user_email} ({user.user_role}).", target_user=user)
        return Response({"message": "Your password has been updated successfully! You may now log in."}, status=status.HTTP_200_OK)


def reset_password_page(request):
    return render(request, 'reset_password.html', {'token': request.GET.get('token', ''), 'email': request.GET.get('email', '')})


def reupload_permit_page(request):
    role_raw = str(request.GET.get('role', 'Merchant')).strip()
    acc_id = str(request.GET.get('id', '')).strip()
    role = 'FoodBank' if role_raw.lower() == 'foodbank' else 'Merchant'
    name, status_val, expiry = '', 'Resubmit Required', '2026-12-31'
    reason = 'Please upload a clear, valid copy of your official permit document.'

    if acc_id.isdigit():
        if role == 'FoodBank':
            fb = FoodBank.objects.filter(org_id=int(acc_id)).first()
            if fb:
                name, status_val, reason, expiry = fb.org_name, fb.status or status_val, fb.status_reason or reason, str(fb.accreditation_expiry or expiry)
        else:
            m = Merchant.objects.filter(merch_id=int(acc_id)).first()
            if m:
                name, status_val, reason, expiry = m.bus_name, m.status or status_val, m.status_reason or reason, str(m.bus_expiry_date or expiry)

    frontend_host = getattr(settings, 'FRONTEND_WEB_URL', 'http://localhost:8081')
    from urllib.parse import urlencode
    query_str = urlencode({'id': acc_id, 'role': role, 'name': name, 'status': status_val, 'reason': reason, 'expiry': expiry})
    return redirect(f"{frontend_host}/establishment/resubmit-permit?{query_str}")


class UserSettingsToggleView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        email = request.GET.get('email', '').strip()
        profile_id = request.GET.get('profile_id', '').strip()
        role = request.GET.get('role', 'Merchant').strip()

        user = None
        if email:
            user = User.objects.filter(user_email__iexact=email).first()
        elif profile_id.isdigit() and int(profile_id) > 0:
            if role.lower() == 'merchant':
                m = Merchant.objects.select_related('user').filter(merch_id=int(profile_id)).first()
                user = m.user if m else None
            elif role.lower() == 'consumer':
                c = Consumer.objects.select_related('user').filter(cons_id=int(profile_id)).first()
                user = c.user if c else None
            elif role.lower() == 'foodbank':
                f = FoodBank.objects.select_related('user').filter(org_id=int(profile_id)).first()
                user = f.user if f else None

        default_settings = {
            "pushNotif": True,
            "smsAlerts": True,
            "emailReports": False,
            "autoDonateExpiry": True,
            "shopOpenTime": "08:00 AM",
            "shopCloseTime": "08:00 PM",
            "shopHoursEnabled": True,
        }
        if not user:
            return Response(default_settings, status=status.HTTP_200_OK)

        try:
            saved = json.loads(user.settings_json or '{}')
            default_settings.update(saved)
        except Exception:
            pass
        return Response(default_settings, status=status.HTTP_200_OK)

    def post(self, request):
        email = str(request.data.get('email', '')).strip()
        profile_id = str(request.data.get('profile_id', '')).strip()
        role = str(request.data.get('role', 'Merchant').strip())
        settings_payload = request.data.get('settings', {})

        user = None
        if email:
            user = User.objects.filter(user_email__iexact=email).first()
        if not user and profile_id.isdigit() and int(profile_id) > 0:
            if role.lower() == 'merchant':
                m = Merchant.objects.select_related('user').filter(merch_id=int(profile_id)).first()
                user = m.user if m else None
            elif role.lower() == 'consumer':
                c = Consumer.objects.select_related('user').filter(cons_id=int(profile_id)).first()
                user = c.user if c else None
            elif role.lower() == 'foodbank':
                f = FoodBank.objects.select_related('user').filter(org_id=int(profile_id)).first()
                user = f.user if f else None

        if not user:
            return Response({"error": "User not found."}, status=status.HTTP_404_NOT_FOUND)

        try:
            current = json.loads(user.settings_json or '{}')
        except Exception:
            current = {}

        if isinstance(settings_payload, dict):
            current.update(settings_payload)
        for direct_key in ("shopOpenTime", "shopCloseTime", "shopHoursEnabled"):
            if direct_key in request.data:
                current[direct_key] = request.data.get(direct_key)

        user.settings_json = json.dumps(current)
        user.save()
        return Response({"message": "Preferences saved.", "settings": current}, status=status.HTTP_200_OK)


class SupportReportSubmitView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        role = str(request.data.get('role', 'User')).strip()
        email = str(request.data.get('email', '')).strip()
        category = str(request.data.get('category', 'General Inquiry')).strip()
        subject = str(request.data.get('subject', '')).strip()
        message = str(request.data.get('message', '')).strip()

        if not subject or not message:
            return Response({"error": "Please provide both a subject and detailed description."}, status=status.HTTP_400_BAD_REQUEST)

        user = User.objects.filter(user_email__iexact=email).first() if email else None
        log_system_activity("Support", f"[{role} Report - {category}] From {email or role}: {subject} — {message}", target_user=user)

        admin_email = getattr(settings, 'EMAIL_HOST_USER', 'reserve.org.com@gmail.com')
        send_compliance_email(
            admin_email,
            f"RESERVE Help & Support Report [{role}: {category}]",
            f"Reporter Role: {role}\nReporter Email: {email}\nCategory: {category}\nSubject: {subject}\n\nDetails:\n{message}"
        )
        return Response({"message": "Your support report has been submitted to the ReServe Admin team."}, status=status.HTTP_201_CREATED)


class MarkOrderNoShowView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        merch_id = request.data.get('merchant_id') or request.data.get('merchant')
        raw_code = str(request.data.get('order_code') or '').strip().upper().split('-')[0]
        force = bool(request.data.get('force', False))

        if not raw_code:
            return Response({"error": "Order / Voucher ID is required."}, status=status.HTTP_400_BAD_REQUEST)

        orders_qs = OrderReservation.objects.select_related(
            'consumer__user',
            'listing__listing__product__merchant__user',
            'listing__listing__product__merchant__plan',
        ).filter(qr_voucher_code__istartswith=raw_code, order_status='Pending')

        if merch_id and str(merch_id).isdigit() and int(merch_id) > 0:
            orders_qs = orders_qs.filter(listing__listing__product__merchant_id=int(merch_id))

        matching_orders = list(orders_qs)
        if not matching_orders:
            return Response({"error": f"No pending order found for voucher {raw_code}."}, status=status.HTTP_404_NOT_FOUND)

        now = timezone.now()
        first_order = matching_orders[0]
        first_safety = first_order.listing.listing if (first_order.listing and first_order.listing.listing) else None
        exp_dt = first_safety.estimated_expiry if first_safety else None

        if exp_dt and timezone.is_naive(exp_dt):
            exp_dt = timezone.make_aware(exp_dt)

        if not force and exp_dt and exp_dt > now:
            exp_fmt = timezone.localtime(exp_dt).strftime('%b %d, %Y %I:%M %p')
            return Response({"error": f"Order {raw_code} cannot be marked as No-Show before its pickup deadline ({exp_fmt})."}, status=status.HTTP_400_BAD_REQUEST)

        merchant = first_order.listing.listing.product.merchant if (first_order.listing and first_order.listing.listing and first_order.listing.listing.product) else None
        consumer = first_order.consumer

        gov = get_governance_config()
        is_pro = bool(merchant and (merchant.plan_id == 2 or (merchant.plan and 'pro' in (merchant.plan.plan_name or '').lower())))
        comm_rate = Decimal(str(gov.get('pro_commission_pct', 4.0))) / Decimal('100') if is_pro else Decimal(str(gov.get('commission_pct', 10.0))) / Decimal('100')

        total_forfeited_deposit = Decimal('0.00')
        total_platform_fee = Decimal('0.00')
        total_merchant_wallet_credit = Decimal('0.00')
        item_summaries = []

        with transaction.atomic():
            for o in matching_orders:
                comm = o.listing
                qty_val = int(o.order_qty or 1)
                if comm:
                    comm.qty_reserved = max(0, int(comm.qty_reserved or 0) - qty_val)
                    comm.save()
                o.order_status = 'No-Show'
                o.save()

                _, _, line_total, _, dep, _, _ = resolve_admin_order_live_pricing(o)
                plat_fee = min(dep, (line_total * comm_rate).quantize(Decimal('0.01')))
                merch_credit = max(Decimal('0.00'), dep - plat_fee)

                total_forfeited_deposit += dep
                total_platform_fee += plat_fee
                total_merchant_wallet_credit += merch_credit

                PaymentTransaction.objects.filter(order=o).update(commission_taken=plat_fee, merchant_earnings=merch_credit)

                p_name = comm.listing.product.prod_name if (comm and comm.listing and comm.listing.product) else f"Order #{o.order_id}"
                item_summaries.append(f"{qty_val}x {p_name}")

            if merchant and total_merchant_wallet_credit > Decimal('0.00'):
                merchant.wallet_bal = Decimal(str(merchant.wallet_bal or 0)) + total_merchant_wallet_credit
                merchant.save()

            strike_limit = int(gov.get('strike_limit', 3))
            current_strikes = 1

            if consumer:
                current_strikes = OrderReservation.objects.filter(consumer=consumer, order_status='No-Show').values('qr_voucher_code').distinct().count()
                if hasattr(consumer, 'strike_count'):
                    consumer.strike_count = current_strikes
                if current_strikes >= strike_limit:
                    consumer.status = 'Suspended'
                    consumer.status_reason = f"Reached {current_strikes}/{strike_limit} No-Show pickup strikes."
                    if consumer.user:
                        consumer.user.is_banned = True
                        consumer.user.save()
                consumer.save()

                if consumer.user:
                    UserNotification.objects.create(
                        user=consumer.user,
                        message=(
                            f"Order {raw_code} ({', '.join(item_summaries)}) at {merchant.bus_name if merchant else 'Merchant'} "
                            f"was marked as an unclaimed No-Show past its pickup deadline. "
                            f"Your 20% commitment fee (₱{float(total_forfeited_deposit):,.2f}) was forfeited and Strike {current_strikes} of {strike_limit} was recorded."
                        ),
                    )

            log_system_activity(
                "Compliance",
                (
                    f"Order {raw_code} ({', '.join(item_summaries)}) marked No-Show by {merchant.bus_name if merchant else 'Merchant'}. "
                    f"Net ₱{float(total_merchant_wallet_credit):,.2f} credited to merchant wallet (ReServe fee: ₱{float(total_platform_fee):,.2f}). "
                    f"Consumer {consumer.cons_fullname if consumer else 'Buyer'} strike {current_strikes}/{strike_limit}."
                ),
                target_user=merchant.user if merchant else None,
                merchant=merchant,
            )

        return Response({
            "message": (
                f"Order {raw_code} marked as No-Show.\n"
                f"• 20% Commitment Fee Forfeited: ₱{float(total_forfeited_deposit):,.2f}\n"
                f"• Net Credited to Your Wallet: ₱{float(total_merchant_wallet_credit):,.2f} (after ₱{float(total_platform_fee):,.2f} platform commission)\n"
                f"• Consumer strike {current_strikes}/{strike_limit} logged with ReServe Admin."
            ),
            "strikes": current_strikes,
            "strike_limit": strike_limit,
        }, status=status.HTTP_200_OK)


class MerchantWithdrawalView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        merch_id = request.GET.get('merchant')
        if not merch_id or not str(merch_id).isdigit():
            return Response([], status=status.HTTP_200_OK)

        qs = WithdrawalReq.objects.filter(merchant_id=int(merch_id)).order_by('-withdrawal_id')
        data = [{
            "withdrawal_id": w.withdrawal_id,
            "id": f"PO-{w.withdrawal_id}",
            "amount": float(w.amount or 0),
            "channel": w.channel or "GCash",
            "account_number": w.gcash_num,
            "account_name": w.account_name or w.merchant.contact_person or w.merchant.bus_name,
            "fee_deducted": float(w.fee_deducted or 0),
            "net_payout": float(w.net_payout or w.amount or 0),
            "reference_number": w.reference_number or "",
            "status": w.withdrawal_status or "Settled",
            "created_at": w.created_at.strftime('%Y-%m-%d %I:%M %p') if w.created_at else timezone.now().strftime('%Y-%m-%d'),
            "processed_at": w.processed_at.strftime('%Y-%m-%d %I:%M %p') if w.processed_at else "",
        } for w in qs]
        return Response(data, status=status.HTTP_200_OK)

    def post(self, request):
        merch_id = request.data.get('merchant_id') or request.data.get('merchant')
        channel = str(request.data.get('channel', 'GCash')).strip()
        account_number = str(request.data.get('account_number', '')).strip()
        account_name = str(request.data.get('account_name', '')).strip()
        try:
            amount = Decimal(str(request.data.get('amount', '0'))).quantize(Decimal('0.01'))
        except Exception:
            return Response({"error": "Invalid withdrawal amount."}, status=status.HTTP_400_BAD_REQUEST)

        merchant = Merchant.objects.select_related('user', 'plan').filter(merch_id=merch_id).first()
        if not merchant:
            return Response({"error": "Merchant account not found."}, status=status.HTTP_404_NOT_FOUND)

        gov = get_governance_config()
        min_withdrawal = Decimal(str(gov.get('min_withdrawal', 100.00))).quantize(Decimal('0.01'))

        if amount < min_withdrawal:
            return Response({"error": f"Minimum withdrawal amount is ₱{float(min_withdrawal):,.2f}."}, status=status.HTTP_400_BAD_REQUEST)

        if amount > (merchant.wallet_bal or Decimal('0.00')):
            return Response({"error": f"Insufficient wallet balance (Available: ₱{float(merchant.wallet_bal or 0):,.2f})."}, status=status.HTTP_400_BAD_REQUEST)

        if not account_number or not account_name:
            return Response({"error": f"Please provide your {channel} account number and registered account name."}, status=status.HTTP_400_BAD_REQUEST)

        fee_deducted = Decimal('0.00')
        net_payout = amount

        ext_id = f"reserve-po-{merchant.merch_id}-{uuid.uuid4().hex[:8]}"
        xendit_ref = disburse_xendit_payout_accounts(
            channel=channel,
            account_number=account_number,
            account_name=account_name,
            net_amount_dec=net_payout,
            external_id=ext_id,
        )

        merchant.wallet_bal = max(Decimal('0.00'), (merchant.wallet_bal or Decimal('0.00')) - amount)
        merchant.save()

        now_ts = timezone.now()
        w_req = WithdrawalReq.objects.create(
            merchant=merchant,
            amount=amount,
            channel=channel,
            gcash_num=account_number,
            account_name=account_name,
            fee_deducted=fee_deducted,
            net_payout=net_payout,
            reference_number=xendit_ref,
            withdrawal_status='Settled',
            processed_at=now_ts,
        )

        log_system_activity(
            "Payout",
            f"Automated Xendit Payout PO-{w_req.withdrawal_id} settled for {merchant.bus_name}: ₱{float(amount):,.2f} (100% disbursed to {channel} {account_number}, ₱0 withdrawal fee, Ref #: {xendit_ref}).",
            target_user=merchant.user,
            merchant=merchant,
            withdrawal=w_req,
        )

        return Response({
            "message": f"Automated Xendit payout of ₱{float(net_payout):,.2f} disbursed to your {channel} account ({account_number}) with ₱0.00 withdrawal fee! (Ref #: {xendit_ref})",
            "withdrawal_id": w_req.withdrawal_id,
            "reference_number": xendit_ref,
            "fee_deducted": 0.0,
            "net_payout": float(net_payout),
        }, status=status.HTTP_201_CREATED)


class UserNotificationsFeedView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        email = request.GET.get('email', '').strip()
        role = request.GET.get('role', 'Consumer').strip()
        profile_id = request.GET.get('profile_id', '').strip()

        user = None
        if email:
            user = User.objects.filter(user_email__iexact=email).first()
        if not user and profile_id.isdigit() and int(profile_id) > 0:
            if role.lower() == 'merchant':
                m = Merchant.objects.select_related('user').filter(merch_id=int(profile_id)).first()
                user = m.user if m else None
            elif role.lower() == 'consumer':
                c = Consumer.objects.select_related('user').filter(cons_id=int(profile_id)).first()
                user = c.user if c else None
            elif role.lower() == 'foodbank':
                f = FoodBank.objects.select_related('user').filter(org_id=int(profile_id)).first()
                user = f.user if f else None

        notifications_data = []
        if user:
            for n in UserNotification.objects.filter(user=user).order_by('-created_at')[:35]:
                n_dt = timezone.localtime(n.created_at) if n.created_at else timezone.now()
                notifications_data.append({
                    "id": f"UN-{n.notif_id}",
                    "type": "Activity",
                    "title": "Official ReServe Notification",
                    "message": n.message,
                    "is_read": n.is_read,
                    "timestamp": n_dt.strftime('%b %d, %Y • %I:%M %p'),
                    "sort_ts": n_dt.timestamp(),
                })

        broadcasts = AdminNotification.objects.filter(admin_notif_type__startswith='Broadcast').order_by('-pk')[:15]
        for b in broadcasts:
            audience = b.admin_notif_type.replace('Broadcast:', '').strip()
            if 'all' in audience.lower() or role.lower() in audience.lower():
                parts = (b.message or '').split(' || ', 1)
                title = parts[0] if len(parts) > 1 else 'System Announcement'
                body = parts[1] if len(parts) > 1 else b.message
                b_dt = timezone.localtime(b.created_at) if getattr(b, 'created_at', None) else timezone.now()
                notifications_data.append({
                    "id": f"BC-{b.pk}",
                    "type": "Announcement",
                    "title": title,
                    "message": body,
                    "audience": audience,
                    "is_read": False,
                    "timestamp": b_dt.strftime('%b %d, %Y • %I:%M %p'),
                    "sort_ts": b_dt.timestamp(),
                })

        notifications_data.sort(key=lambda x: float(x.get("sort_ts") or 0), reverse=True)
        return Response({"notifications": notifications_data}, status=status.HTTP_200_OK)


class UserRegistrationView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    parser_classes = (MultiPartParser, FormParser)

    def post(self, request):
        sync_table_sequence('user', 'user_id')
        sync_table_sequence('location', 'loc_id')
        sync_table_sequence('consumer', 'cons_id')
        sync_table_sequence('food_bank', 'org_id')

        raw_phone = str(request.data.get('phone_number') or request.data.get('phone') or '').strip()
        if raw_phone and is_phone_already_registered(raw_phone):
            return Response({"error": "This phone number is already registered to an existing RESERVE account."}, status=status.HTTP_400_BAD_REQUEST)

        serializer = UserRegistrationSerializer(data=request.data)

        if serializer.is_valid():
            with transaction.atomic():
                user = serializer.save()
                role = user.user_role.lower()
                address = request.data.get('address', 'Cebu City, Cebu')

                try:
                    lat = float(request.data.get('latitude') or 10.3157)
                    lng = float(request.data.get('longitude') or 123.8854)
                except (ValueError, TypeError):
                    lat, lng = 10.3157, 123.8854

                Location.objects.create(user=user, address_name=address, latitude=lat, longitude=lng, is_primary=True)

                if role == 'consumer':
                    full_name = request.data.get('full_name', 'Unknown User')
                    phone = request.data.get('phone_number', '')
                    Consumer.objects.create(user=user, cons_fullname=full_name, phone_num=phone, address=address)
                    log_system_activity("Registration", f"New Consumer registered: {full_name} ({user.user_email}).", target_user=user)
                elif role == 'foodbank':
                    org_name = request.data.get('org_name', 'Community Food Bank')
                    contact_person = request.data.get('contact_person', '')
                    hotline = request.data.get('hotline', '')
                    phone = request.data.get('phone', '')
                    cert_file = request.FILES.get('accreditation_path')
                    saved_path = default_storage.save(cert_file.name, cert_file) if cert_file else 'default_cert.jpg'

                    FoodBank.objects.create(
                        user=user,
                        org_name=org_name,
                        address=address,
                        contact_person=contact_person,
                        hotline=hotline,
                        phone=phone,
                        accreditation_path=saved_path,
                        status='Pending'
                    )
                    log_system_activity("KYB", f"New Food Bank registration: {org_name} awaiting verification.", target_user=user)

            return Response({"message": "Account created successfully", "user_id": user.user_id, "role": user.user_role}, status=status.HTTP_201_CREATED)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class ScanBusinessPermitOCRView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    parser_classes = (MultiPartParser, FormParser, JSONParser)

    def post(self, request):
        import base64
        from .utils import _resolve_permit_disk_path, _prepare_image_bytes_under_1mb

        merch_id = request.data.get("merchant_id") or request.GET.get("merchant_id")
        permit_file = request.FILES.get("bus_permit_path") or request.FILES.get("permit")

        expected_bus_name = str(request.data.get("bus_name") or "").strip()
        expected_address = str(request.data.get("address") or "").strip()

        if merch_id and str(merch_id).isdigit() and not permit_file:
            merchant = Merchant.objects.filter(merch_id=int(merch_id)).first()
            if not merchant or not merchant.bus_permit_path:
                return Response({"error": "Merchant permit file not found."}, status=status.HTTP_404_NOT_FOUND)

            try:
                meta = json.loads(merchant.ocr_extracted_text or "{}")
            except Exception:
                meta = {}

            raw_path_str = str(merchant.bus_permit_path).strip()
            permit_data_url = ""

            if raw_path_str.startswith("http://") or raw_path_str.startswith("https://"):
                permit_data_url = raw_path_str
            else:
                try:
                    disk_path = _resolve_permit_disk_path(raw_path_str)
                    img_bytes, _ = _prepare_image_bytes_under_1mb(disk_path)
                    b64_str = base64.b64encode(img_bytes).decode("ascii")
                    permit_data_url = f"data:image/jpeg;base64,{b64_str}"
                except Exception as img_err:
                    print("Permit preview base64 notice:", img_err)

            ocr_results = {}
            try:
                ocr_results = extract_cebu_permit_data(
                    raw_path_str,
                    expected_bus_name=merchant.bus_name,
                    expected_address=merchant.address,
                ) or {}
            except Exception as scan_err:
                print("Admin permit scan notice:", scan_err)

            for k in (
                "permit_number",
                "business_name",
                "business_address",
                "nature_of_business",
                "tin_number",
                "capital",
                "calendar_year",
                "representative",
                "expiry_date",
            ):
                val_new = ocr_results.get(k)
                val_old = meta.get(k)
                if val_new and val_new not in ("N/A", "Not Detected", "Unverified"):
                    meta[k] = val_new
                elif val_old and val_old not in ("N/A", "Not Detected", "Unverified"):
                    ocr_results[k] = val_old

            if not ocr_results.get("business_name") or ocr_results.get("business_name") == "N/A":
                ocr_results["business_name"] = merchant.bus_name
            if not ocr_results.get("business_address") or ocr_results.get("business_address") == "N/A":
                ocr_results["business_address"] = merchant.address or "Cebu City, Philippines"
            if (not ocr_results.get("representative") or ocr_results.get("representative") == "N/A") and merchant.contact_person:
                ocr_results["representative"] = merchant.contact_person

            eligibility_report = build_cebu_permit_eligibility_report(
                ocr_results,
                expected_bus_name=merchant.bus_name,
                expected_address=merchant.address,
            )

            meta["eligibility_report"] = eligibility_report
            merchant.ocr_extracted_text = json.dumps(meta)
            merchant.save(update_fields=["ocr_extracted_text"])

            ocr_results.pop("raw_text", None)
            ocr_results.pop("visual_info", None)
            ocr_results["eligibility_report"] = eligibility_report

            return Response({
                "message": eligibility_report.get("summary", "Permit scanned."),
                "ocr_data": ocr_results,
                "eligibility_report": eligibility_report,
                "permit_data_url": permit_data_url,
            }, status=status.HTTP_200_OK)

        if not permit_file:
            return Response({"error": "Please select a Business Permit image to scan."}, status=status.HTTP_400_BAD_REQUEST)

        ocr_results = extract_cebu_permit_data(
            permit_file,
            expected_bus_name=expected_bus_name,
            expected_address=expected_address,
        )
        eligibility_report = ocr_results.get("eligibility_report") or evaluate_permit_eligibility(
            ocr_results, expected_bus_name, expected_address
        )
        ocr_results.pop("raw_text", None)
        ocr_results.pop("visual_info", None)
        ocr_results["eligibility_report"] = eligibility_report

        return Response({
            "message": eligibility_report.get("summary", "Permit scanned."),
            "ocr_data": ocr_results,
            "eligibility_report": eligibility_report,
        }, status=status.HTTP_200_OK)


class MerchantRegistrationView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    parser_classes = (MultiPartParser, FormParser)

    def post(self, request):
        sync_table_sequence('user', 'user_id')
        sync_table_sequence('location', 'loc_id')
        sync_table_sequence('merchant', 'merch_id')

        bus_name_check = str(request.data.get('bus_name') or '').strip()
        address_check = str(request.data.get('address') or '').strip()
        lat_check = request.data.get('latitude')
        lng_check = request.data.get('longitude')
        phone_check = str(request.data.get('phone') or '').strip()

        if bus_name_check:
            dup_merchant = find_duplicate_pending_merchant(bus_name_check, address_check, lat_check, lng_check)
            if dup_merchant:
                return Response({
                    "error": f"An application for '{dup_merchant.bus_name}' at this location has already been submitted and is awaiting Admin approval."
                }, status=status.HTTP_400_BAD_REQUEST)

        if phone_check and is_phone_already_registered(phone_check):
            return Response({"error": "This phone number is already registered to an existing RESERVE account."}, status=status.HTTP_400_BAD_REQUEST)

        serializer = MerchantRegistrationSerializer(data=request.data)

        if serializer.is_valid():
            merchant = serializer.save()

            bus_name = str(request.data.get('bus_name') or '').strip()
            address = str(request.data.get('address') or '').strip()
            contact_person = str(request.data.get('contact_person') or '').strip()
            permit_number = str(request.data.get('permit_number') or '').strip()
            line_of_business = str(request.data.get('line_of_business') or request.data.get('nature_of_business') or '').strip()
            tin_number = str(request.data.get('tin_number') or '').strip()
            calendar_year = str(request.data.get('calendar_year') or '').strip()
            expiry_date = str(request.data.get('bus_expiry_date') or '').strip()
            shop_open_time = str(request.data.get('shop_open_time') or '08:00 AM').strip()
            shop_close_time = str(request.data.get('shop_close_time') or '08:00 PM').strip()

            if bus_name:
                merchant.bus_name = bus_name
            if address:
                merchant.address = address
            if contact_person:
                merchant.contact_person = contact_person

            raw_ocr = {}
            if merchant.bus_permit_path:
                try:
                    raw_ocr = extract_cebu_permit_data(
                        str(merchant.bus_permit_path),
                        expected_bus_name=merchant.bus_name,
                        expected_address=merchant.address,
                    ) or {}
                except Exception as e:
                    print("Permit OCR scan during registration notice:", e)

            eligibility_report = raw_ocr.get("eligibility_report") or evaluate_permit_eligibility(
                raw_ocr,
                expected_bus_name=merchant.bus_name,
                expected_address=merchant.address,
            )

            resolved_cal_year = calendar_year or raw_ocr.get("calendar_year") or "2026-2027"
            if not expiry_date:
                expiry_date = raw_ocr.get("expiry_date") or ""
            if not expiry_date and resolved_cal_year:
                yr_match = re.findall(r'20\d{2}', resolved_cal_year)
                if yr_match:
                    expiry_date = f"{max(int(y) for y in yr_match)}-12-31"
            if not expiry_date:
                expiry_date = "2026-12-31"

            merchant.bus_expiry_date = expiry_date

            permit_metadata = {
                "business_name": merchant.bus_name,
                "business_address": merchant.address or raw_ocr.get("business_address") or "Cebu City, Philippines",
                "permit_number": permit_number or raw_ocr.get("permit_number") or "N/A",
                "nature_of_business": line_of_business or raw_ocr.get("nature_of_business") or merchant.merch_type,
                "tin_number": tin_number or raw_ocr.get("tin_number") or "N/A",
                "capital": raw_ocr.get("capital") or "",
                "calendar_year": resolved_cal_year,
                "representative": merchant.contact_person or raw_ocr.get("representative") or "N/A",
                "expiry_date": str(merchant.bus_expiry_date),
                "lgu_anchor": raw_ocr.get("lgu_anchor") or "Cebu City Government",
                "shop_open_time": shop_open_time,
                "shop_close_time": shop_close_time,
                "eligibility_report": eligibility_report,
            }

            merchant.ocr_extracted_text = json.dumps(permit_metadata)
            merchant.save()

            if merchant.user:
                try:
                    u_settings = json.loads(merchant.user.settings_json or '{}')
                except Exception:
                    u_settings = {}
                u_settings.setdefault("shopOpenTime", shop_open_time)
                u_settings.setdefault("shopCloseTime", shop_close_time)
                u_settings.setdefault("shopHoursEnabled", True)
                merchant.user.settings_json = json.dumps(u_settings)
                merchant.user.save(update_fields=['settings_json'])

            try:
                lat = float(request.data.get('latitude') or 10.3157)
                lng = float(request.data.get('longitude') or 123.8854)
            except (ValueError, TypeError):
                lat, lng = 10.3157, 123.8854

            Location.objects.create(user=merchant.user, address_name=merchant.address or 'Cebu City, Cebu', latitude=lat, longitude=lng, is_primary=True)

            log_system_activity(
                "KYB",
                f"New merchant application submitted: {merchant.bus_name} (Permit #{permit_metadata['permit_number']} • AI Score: {eligibility_report.get('authenticity_score', 0)}%).",
                target_user=merchant.user,
                merchant=merchant,
            )

            return Response({
                "message": "Merchant registration submitted successfully. Under review by admin.",
                "merchant_id": merchant.merch_id,
                "bus_name": merchant.bus_name,
                "address": merchant.address,
                "permit_details": permit_metadata,
                "eligibility_report": eligibility_report,
            }, status=status.HTTP_201_CREATED)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class CustomLoginView(TokenObtainPairView):
    serializer_class = CustomLoginSerializer

    def post(self, request, *args, **kwargs):
        res = super().post(request, *args, **kwargs)
        if res.status_code == 200 and isinstance(res.data, dict):
            email = res.data.get('email') or request.data.get('user_email') or ''
            role = res.data.get('role') or 'User'
            user_obj = User.objects.filter(user_email__iexact=str(email).strip()).first()
            if user_obj:
                try:
                    UserSession.objects.create(
                        user=user_obj,
                        ses_token=str(res.data.get('access', ''))[:240],
                        ses_info=f"{role} login from {request.META.get('REMOTE_ADDR', '127.0.0.1')}",
                    )
                except Exception:
                    pass
                if role == 'Admin':
                    log_system_activity("Security", f"Super Administrator ({user_obj.user_email}) authenticated into the Enterprise Console.")
        return res


class DocumentResubmissionView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    parser_classes = (MultiPartParser, FormParser, JSONParser)

    def get(self, request, merch_id):
        role = str(request.GET.get('role', 'Merchant')).strip()
        if role.lower() == 'foodbank':
            fb = FoodBank.objects.select_related('user').filter(org_id=merch_id).first()
            if not fb:
                return Response({"error": "Food Bank not found."}, status=status.HTTP_404_NOT_FOUND)
            return Response({
                "id": fb.org_id,
                "role": "FoodBank",
                "bus_name": fb.org_name,
                "address": fb.address or "Cebu City",
                "contact_person": fb.contact_person or "",
                "status": fb.status,
                "reason": fb.status_reason or "Please upload a clear copy of your accreditation certificate.",
                "expiry_date": str(fb.accreditation_expiry or "2026-12-31"),
            }, status=status.HTTP_200_OK)

        merchant = Merchant.objects.select_related('user').filter(merch_id=merch_id).first()
        if not merchant:
            return Response({"error": "Merchant not found."}, status=status.HTTP_404_NOT_FOUND)

        try:
            meta = json.loads(merchant.ocr_extracted_text or '{}')
        except Exception:
            meta = {}

        return Response({
            "id": merchant.merch_id,
            "role": "Merchant",
            "bus_name": meta.get("business_name") or merchant.bus_name,
            "address": meta.get("business_address") or merchant.address or "",
            "permit_number": meta.get("permit_number") if meta.get("permit_number") != "N/A" else "",
            "line_of_business": meta.get("nature_of_business") or merchant.merch_type or "",
            "tin_number": meta.get("tin_number") if meta.get("tin_number") != "N/A" else "",
            "calendar_year": meta.get("calendar_year") or "2026-2027",
            "contact_person": meta.get("representative") or merchant.contact_person or "",
            "status": merchant.status,
            "reason": merchant.status_reason or "Please upload a clear, valid copy of your Cebu City Business Permit.",
            "expiry_date": str(merchant.bus_expiry_date or "2026-12-31"),
        }, status=status.HTTP_200_OK)

    def post(self, request, merch_id):
        role = str(request.data.get('role', 'Merchant')).strip()
        permit_file = request.FILES.get('bus_permit_path') or request.FILES.get('permit') or request.FILES.get('accreditation_path')

        if not permit_file:
            return Response({"error": "Please attach a valid permit or certificate image file."}, status=status.HTTP_400_BAD_REQUEST)

        saved_path = default_storage.save(permit_file.name, permit_file)

        if role.lower() == 'foodbank':
            try:
                fb = FoodBank.objects.select_related('user').get(org_id=merch_id)
            except FoodBank.DoesNotExist:
                return Response({"error": "Food Bank not found."}, status=status.HTTP_404_NOT_FOUND)

            fb.accreditation_path = saved_path
            fb.status = 'Pending'
            fb.status_reason = ''
            fb.save()

            log_system_activity("KYB", f"Food Bank {fb.org_name} resubmitted accreditation document for review.", target_user=fb.user)
            return Response({"message": "Document resubmitted successfully! Your account is now back under Admin review."}, status=status.HTTP_200_OK)

        try:
            merchant = Merchant.objects.select_related('user').get(merch_id=merch_id)
        except Merchant.DoesNotExist:
            return Response({"error": "Merchant not found."}, status=status.HTTP_404_NOT_FOUND)

        merchant.bus_permit_path = saved_path

        bus_name = str(request.data.get('bus_name') or '').strip()
        contact_person = str(request.data.get('contact_person') or '').strip()
        permit_number = str(request.data.get('permit_number') or '').strip()
        line_of_business = str(request.data.get('line_of_business') or request.data.get('nature_of_business') or '').strip()
        tin_number = str(request.data.get('tin_number') or '').strip()
        calendar_year = str(request.data.get('calendar_year') or '').strip()
        expiry_date = str(request.data.get('bus_expiry_date') or '').strip()

        if bus_name:
            merchant.bus_name = bus_name
        if contact_person:
            merchant.contact_person = contact_person

        try:
            existing_meta = json.loads(merchant.ocr_extracted_text or '{}')
        except Exception:
            existing_meta = {}

        raw_ocr = {}
        try:
            raw_ocr = extract_cebu_permit_data(
                str(merchant.bus_permit_path),
                expected_bus_name=merchant.bus_name,
                expected_address=merchant.address,
            ) or {}
        except Exception as e:
            print("Resubmit permit OCR scan notice:", e)

        eligibility_report = raw_ocr.get("eligibility_report") or evaluate_permit_eligibility(
            raw_ocr,
            expected_bus_name=merchant.bus_name,
            expected_address=merchant.address,
        )

        resolved_cal_year = calendar_year or raw_ocr.get("calendar_year") or existing_meta.get("calendar_year") or "2026-2027"
        if not expiry_date:
            expiry_date = raw_ocr.get("expiry_date") or ""
        if not expiry_date and resolved_cal_year:
            yr_match = re.findall(r'20\d{2}', resolved_cal_year)
            if yr_match:
                expiry_date = f"{max(int(y) for y in yr_match)}-12-31"
        if expiry_date:
            merchant.bus_expiry_date = expiry_date

        permit_metadata = {
            "business_name": merchant.bus_name,
            "business_address": merchant.address or raw_ocr.get("business_address") or "Cebu City, Philippines",
            "permit_number": permit_number or raw_ocr.get("permit_number") or existing_meta.get("permit_number") or "N/A",
            "nature_of_business": line_of_business or raw_ocr.get("nature_of_business") or existing_meta.get("nature_of_business") or merchant.merch_type,
            "tin_number": tin_number or raw_ocr.get("tin_number") or existing_meta.get("tin_number") or "N/A",
            "capital": raw_ocr.get("capital") or existing_meta.get("capital") or "",
            "calendar_year": resolved_cal_year,
            "representative": merchant.contact_person or raw_ocr.get("representative") or "N/A",
            "expiry_date": str(merchant.bus_expiry_date or "2026-12-31"),
            "lgu_anchor": raw_ocr.get("lgu_anchor") or "Cebu City Government",
            "shop_open_time": existing_meta.get("shop_open_time", "08:00 AM"),
            "shop_close_time": existing_meta.get("shop_close_time", "08:00 PM"),
            "eligibility_report": eligibility_report,
        }

        merchant.ocr_extracted_text = json.dumps(permit_metadata)
        merchant.status = 'Pending'
        merchant.status_reason = ''
        merchant.is_verified = False
        merchant.save()

        log_system_activity(
            "KYB",
            f"Resubmitted/Renewed permit from {merchant.bus_name} (Permit #{permit_metadata['permit_number']} • AI Score: {eligibility_report.get('authenticity_score', 0)}%).",
            target_user=merchant.user,
            merchant=merchant,
        )

        return Response({
            "message": "Permit and updated business details submitted! Your application is now pending Admin verification.",
            "permit_details": permit_metadata,
            "eligibility_report": eligibility_report,
        }, status=status.HTTP_200_OK)


class AdminMerchantApprovalView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        pending_merchants = Merchant.objects.filter(status='Pending')
        data = [{
            "merchant_id": m.merch_id,
            "business_name": m.bus_name,
            "merchant_type": m.merch_type,
            "expiry_date": m.bus_expiry_date,
            "status": m.status,
            "permit_path": str(m.bus_permit_path)
        } for m in pending_merchants]
        return Response({"pending_merchants": data}, status=status.HTTP_200_OK)

    def post(self, request, merch_id):
        return self.patch(request, merch_id)

    def patch(self, request, merch_id):
        action = str(request.data.get('action') or '').strip().lower()
        role_raw = str(request.data.get('role') or 'Merchant').strip().lower()
        reason = str(
            request.data.get('reason')
            or request.data.get('message')
            or request.data.get('body')
            or request.data.get('notice')
            or ''
        ).strip()

        clean_id_str = re.sub(r'\D', '', str(merch_id or ''))
        target_id = int(clean_id_str) if clean_id_str.isdigit() else 0

        host_scheme = request.scheme or "http"
        host_domain = request.get_host() or "127.0.0.1:8000"
        host_base = f"{host_scheme}://{host_domain}"

        if 'cons' in role_raw:
            try:
                consumer = Consumer.objects.select_related('user').get(cons_id=target_id)
            except Consumer.DoesNotExist:
                return Response({"error": "Consumer not found"}, status=status.HTTP_404_NOT_FOUND)

            if action == 'suspend':
                consumer.status = 'Suspended'
                consumer.status_reason = reason
                consumer.save()
                consumer.user.is_banned = True
                consumer.user.save()

                appeal_link = f"{host_base}/appeal/?role=consumer&id={consumer.cons_id}"
                send_compliance_email(
                    consumer.user.user_email,
                    "ReServe Account Suspended",
                    f"Hello {consumer.cons_fullname},\n\nYour account has been suspended due to: {reason}.\n"
                    f"If you wish to appeal this decision, please submit an appeal here: {appeal_link}"
                )
                log_system_activity("Compliance", f"Consumer CID-{consumer.cons_id} ({consumer.cons_fullname}) suspended: {reason}.", target_user=consumer.user)
                return Response({"message": f"Consumer {consumer.cons_fullname} has been suspended."}, status=status.HTTP_200_OK)

            elif action in ('approve', 'activate', 'unsuspend'):
                consumer.status = 'Active'
                consumer.status_reason = ''
                consumer.save()
                consumer.user.is_banned = False
                consumer.user.save()
                log_system_activity("Approved", f"Consumer CID-{consumer.cons_id} ({consumer.cons_fullname}) reactivated by Admin.", target_user=consumer.user)
                return Response({"message": f"Consumer {consumer.cons_fullname} reactivated."}, status=status.HTTP_200_OK)

            elif action in ('notify', 'send_notification', 'message'):
                if not reason:
                    return Response({"error": "Please enter a notification message."}, status=status.HTTP_400_BAD_REQUEST)
                send_compliance_email(consumer.user.user_email, "Official Notice from ReServe Admin", f"Hello {consumer.cons_fullname},\n\nMessage from ReServe Admin:\n\"{reason}\"")
                if consumer.phone_num:
                    e164, _ = normalize_ph_phone(consumer.phone_num)
                    if e164:
                        send_twilio_sms(e164, f"RESERVE Admin Notice: {reason}")
                log_system_activity("Compliance", f"Admin Notice to {consumer.cons_fullname}: {reason}", target_user=consumer.user)
                return Response({"message": f"Notification sent to {consumer.cons_fullname}."}, status=status.HTTP_200_OK)

            return Response({"error": "Invalid Consumer action"}, status=status.HTTP_400_BAD_REQUEST)

        if any(k in role_raw for k in ('food', 'ngo', 'fb')):
            try:
                fb = FoodBank.objects.select_related('user').get(org_id=target_id)
            except FoodBank.DoesNotExist:
                return Response({"error": "Food Bank not found"}, status=status.HTTP_404_NOT_FOUND)

            if action in ('approve', 'activate', 'unsuspend'):
                fb.status = 'Active'
                fb.status_reason = ''
                fb.save()
                fb.user.is_banned = False
                fb.user.save()
                send_compliance_email(fb.user.user_email, "ReServe Food Bank Approved", f"Hello {fb.org_name},\n\nYour organization has been verified and approved on ReServe!")
                log_system_activity("Approved", f"Food Bank FB-{fb.org_id} ({fb.org_name}) approved and activated.", target_user=fb.user)
                return Response({"message": f"Food Bank {fb.org_name} approved."}, status=status.HTTP_200_OK)

            elif action == 'reject':
                fb.status = 'Resubmit Required'
                fb.status_reason = reason
                fb.save()
                reupload_link = f"{host_base}/reupload/?role=foodbank&id={fb.org_id}"
                send_compliance_email(
                    fb.user.user_email,
                    "Action Required: ReServe Food Bank Verification",
                    f"Hello {fb.org_name},\n\nYour accreditation document could not be verified due to: {reason}.\n"
                    f"Please log into your account or use this link to upload a clearer copy: {reupload_link}"
                )
                log_system_activity("Compliance", f"Food Bank FB-{fb.org_id} ({fb.org_name}) requested to resubmit accreditation: {reason}.", target_user=fb.user)
                return Response({"message": f"Food Bank {fb.org_name} flagged for resubmission."}, status=status.HTTP_200_OK)

            elif action == 'suspend':
                fb.status = 'Suspended'
                fb.status_reason = reason
                fb.save()
                fb.user.is_banned = True
                fb.user.save()
                appeal_link = f"{host_base}/appeal/?role=foodbank&id={fb.org_id}"
                send_compliance_email(
                    fb.user.user_email,
                    "ReServe Food Bank Account Suspended",
                    f"Hello {fb.org_name},\n\nYour account has been suspended due to: {reason}.\n"
                    f"To reactivate your account, submit an appeal here: {appeal_link}"
                )
                log_system_activity("Compliance", f"Food Bank FB-{fb.org_id} ({fb.org_name}) suspended: {reason}.", target_user=fb.user)
                return Response({"message": f"Food Bank {fb.org_name} suspended."}, status=status.HTTP_200_OK)

            elif action in ('notify', 'send_notification', 'message'):
                if not reason:
                    return Response({"error": "Please enter a notification message."}, status=status.HTTP_400_BAD_REQUEST)
                send_compliance_email(fb.user.user_email, "Important Notice from ReServe Admin", f"Hello {fb.org_name},\n\nOfficial Notice from ReServe Admin:\n\"{reason}\"")
                if fb.phone:
                    e164, _ = normalize_ph_phone(fb.phone)
                    if e164:
                        send_twilio_sms(e164, f"RESERVE Admin Notice ({fb.org_name}): {reason}")
                log_system_activity("Compliance", f"Admin Notice to {fb.org_name}: {reason}", target_user=fb.user)
                return Response({"message": f"Notification sent to {fb.org_name}."}, status=status.HTTP_200_OK)

            return Response({"error": "Invalid Food Bank action"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            merchant = Merchant.objects.select_related('user').get(merch_id=target_id)
        except Merchant.DoesNotExist:
            return Response({"error": "Merchant not found"}, status=status.HTTP_404_NOT_FOUND)

        if action in ('approve', 'activate', 'unsuspend'):
            merchant.is_verified = True
            merchant.status = 'Active'
            merchant.status_reason = ''
            merchant.save()
            merchant.user.is_banned = False
            merchant.user.save()

            log_system_activity("Approved", f"Merchant MID-{merchant.merch_id} ({merchant.bus_name}) approved by Admin.", target_user=merchant.user, merchant=merchant)
            send_compliance_email(merchant.user.user_email, "ReServe Merchant Application Approved", f"Hello {merchant.bus_name},\n\nYour business permit has been verified and your account is now Active!")
            return Response({
                "message": f"Merchant {merchant.bus_name} has been approved successfully.",
                "bus_name": merchant.bus_name,
                "address": merchant.address or "",
            }, status=status.HTTP_200_OK)

        elif action == 'reject':
            merchant.is_verified = False
            merchant.status = 'Resubmit Required'
            merchant.status_reason = reason
            merchant.save()

            reupload_link = f"{host_base}/reupload/?role=merchant&id={merchant.merch_id}"
            send_compliance_email(
                merchant.user.user_email,
                "Action Required: ReServe Business Permit Verification",
                f"Hello {merchant.bus_name},\n\nYour application requires attention due to: {reason}.\n"
                f"Your registration details have been saved. Please log in or click here to upload a new document copy: {reupload_link}"
            )
            log_system_activity("Compliance", f"Merchant MID-{merchant.merch_id} ({merchant.bus_name}) requested to resubmit permit: {reason}.", target_user=merchant.user, merchant=merchant)
            return Response({"message": f"Merchant {merchant.bus_name} notified to resubmit document."}, status=status.HTTP_200_OK)

        elif action == 'suspend':
            merchant.is_verified = False
            merchant.status = 'Suspended'
            merchant.status_reason = reason
            merchant.save()
            merchant.user.is_banned = True
            merchant.user.save()

            appeal_link = f"{host_base}/appeal/?role=merchant&id={merchant.merch_id}"
            send_compliance_email(
                merchant.user.user_email,
                "ReServe Merchant Account Suspended",
                f"Hello {merchant.bus_name},\n\nYour merchant account has been suspended. Reason: {reason}.\n"
                f"If you wish to appeal and reactivate your account, click here: {appeal_link}"
            )
            log_system_activity("Compliance", f"Merchant MID-{merchant.merch_id} ({merchant.bus_name}) suspended: {reason}.", target_user=merchant.user, merchant=merchant)
            return Response({"message": f"Merchant {merchant.bus_name} has been suspended."}, status=status.HTTP_200_OK)

        elif action in ('notify', 'send_notification', 'message'):
            if not reason:
                return Response({"error": "Please enter a notification message."}, status=status.HTTP_400_BAD_REQUEST)
            is_renewal = bool(request.data.get('is_renewal', False))
            if is_renewal:
                merchant.status = 'Renewal Required'
                merchant.status_reason = reason
                merchant.save()

            log_system_activity("Compliance", f"Compliance Notice to {merchant.bus_name}: {reason}", target_user=merchant.user, merchant=merchant)
            send_compliance_email(
                merchant.user.user_email,
                "Official Compliance Notice - ReServe Admin",
                f'Hello {merchant.bus_name},\n\nMessage from ReServe Admin:\n"{reason}"\n\n'
                f"Please log into your ReServe Merchant Portal to take any necessary action."
            )
            if merchant.phone:
                e164, _ = normalize_ph_phone(merchant.phone)
                if e164:
                    send_twilio_sms(e164, f"RESERVE Admin Notice ({merchant.bus_name}): {reason}")
            return Response({"message": f"Notification sent to {merchant.bus_name}."}, status=status.HTTP_200_OK)

        return Response({"error": "Invalid action parameter."}, status=status.HTTP_400_BAD_REQUEST)


def get_dashboard_recent_marketplace_records(limit=10):
    """
    Returns up to `limit` (default 10) real marketplace records across:
    1) Consumer Order Reservations (OID...) with live dynamic 20%/80% pricing
    2) Active Surplus Food Listings (FL-...)
    3) Surplus Donations (DON-...)
    Each record includes `record_json` so the Dashboard 'View' button opens the Digital Receipt modal.
    """
    from listings.models import PerishableProduct, ListingSafety
    from donations.models import Donation

    now_dt = timezone.now()
    combined = []

    recent_orders = list(
        OrderReservation.objects.select_related(
            "consumer__user",
            "listing__listing__product__merchant",
            "listing__listing__product__category",
            "listing__listing__product__unit",
        ).order_by("-ordered_at", "-order_id")[:30]
    )
    order_ids = [o.order_id for o in recent_orders]
    tx_map = {
        t.order_id: t
        for t in PaymentTransaction.objects.filter(order_id__in=order_ids)
    }

    grouped_orders = {}
    for o in recent_orders:
        b_code = str(o.qr_voucher_code or f"OID{100 + o.order_id}").strip().upper().split("-")[0]
        comm = o.listing
        saf = comm.listing if comm else None
        prod = saf.product if saf else None
        merch = prod.merchant if prod else None

        (
            u_price,
            o_price,
            line_tot,
            _,
            dep_paid,
            rem_bal,
            gw_ref,
        ) = resolve_admin_order_live_pricing(o, tx_map)

        cat_name = prod.category.category_name if (prod and prod.category) else "Surplus"
        item_entry = {
            "name": prod.prod_name if prod else f"Order Item #{o.order_id}",
            "category": cat_name,
            "qty": o.order_qty,
            "unit_price": f"₱{float(u_price):,.2f}",
            "orig_price": f"₱{float(o_price):,.2f}",
            "subtotal": f"₱{float(line_tot):,.2f}",
        }

        o_local = timezone.localtime(o.ordered_at) if o.ordered_at else now_dt
        exp_str = (
            timezone.localtime(saf.estimated_expiry).strftime("%b %d, %Y • %I:%M %p")
            if (saf and saf.estimated_expiry)
            else "Standard Window"
        )

        if b_code not in grouped_orders:
            grouped_orders[b_code] = {
                "id": b_code,
                "receipt_type": "Consumer Order Reservation (20% Online / 80% Cash)",
                "gateway_ref": gw_ref or f"XND-{b_code}",
                "items_list": [item_entry],
                "total_qty": int(o.order_qty or 1),
                "establishment": merch.bus_name if merch else "Partner Merchant",
                "merchant_phone": (merch.phone or merch.hotline or "Verified Store") if merch else "Verified Store",
                "address": (merch.address if (merch and merch.address) else "Cebu City"),
                "buyer": o.consumer.cons_fullname if o.consumer else "Verified Consumer",
                "buyer_phone": (o.consumer.phone_num if (o.consumer and o.consumer.phone_num) else "Verified Buyer"),
                "buyer_email": (o.consumer.user.user_email if (o.consumer and o.consumer.user) else ""),
                "date": o_local.strftime("%b %d, %Y • %I:%M %p"),
                "pickup_deadline": exp_str,
                "total_dec": line_tot,
                "dep_dec": dep_paid,
                "rem_dec": rem_bal,
                "status": o.order_status or "Pending",
                "sort_ts": float(o_local.timestamp()),
            }
        else:
            g = grouped_orders[b_code]
            g["items_list"].append(item_entry)
            g["total_qty"] += int(o.order_qty or 1)
            g["total_dec"] += line_tot
            g["dep_dec"] += dep_paid
            g["rem_dec"] += rem_bal

    for b_code, g in grouped_orders.items():
        its = g["items_list"]
        if len(its) == 1:
            title_str = f"{its[0]['qty']}x {its[0]['name']}"
        else:
            title_str = f"{its[0]['qty']}x {its[0]['name']} +{len(its) - 1} more"

        rec = {
            "id": b_code,
            "record_kind": "Order",
            "receipt_type": g["receipt_type"],
            "gateway_ref": g["gateway_ref"],
            "title": title_str,
            "items_breakdown": its,
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
            "amount": f"₱{float(g['total_dec']):,.2f} (20% Dep: ₱{float(g['dep_dec']):,.2f})",
            "status": g["status"],
            "sort_ts": g["sort_ts"],
        }
        rec["record_json"] = json.dumps(rec)
        combined.append(rec)

    safety_map = {
        s.product_id: s
        for s in ListingSafety.objects.select_related("commerce").all()
    }
    for prod in PerishableProduct.objects.select_related("category", "unit", "merchant").order_by("-prod_id")[:15]:
        s_rec = safety_map.get(prod.prod_id)
        comm = getattr(s_rec, "commerce", None) if s_rec else None
        if not comm:
            continue
        qty_avail = int(comm.qty_available or 0)
        disc_p = Decimal(str(comm.current_price or comm.discounted_price or 0)).quantize(Decimal("0.01"))
        orig_p = Decimal(str(comm.orig_price or disc_p)).quantize(Decimal("0.01"))
        dep_20 = (disc_p * Decimal("0.20")).quantize(Decimal("0.01"))
        rem_80 = max(Decimal("0.00"), disc_p - dep_20)
        u_name = prod.unit.unit_name if prod.unit else "pcs"
        exp_dt = s_rec.estimated_expiry if s_rec else None
        exp_str = timezone.localtime(exp_dt).strftime("%b %d, %Y • %I:%M %p") if exp_dt else "Standard Window"

        rec = {
            "id": f"FL-{prod.prod_id}",
            "record_kind": "Listing",
            "receipt_type": "Surplus Food Listing Inventory Record",
            "gateway_ref": f"LISTING-FL-{prod.prod_id}",
            "title": f"{prod.prod_name} ({qty_avail} {u_name})",
            "items_breakdown": [
                {
                    "name": prod.prod_name,
                    "category": prod.category.category_name if prod.category else "Surplus",
                    "qty": qty_avail,
                    "unit_price": f"₱{float(disc_p):,.2f}",
                    "orig_price": f"₱{float(orig_p):,.2f}",
                    "subtotal": f"₱{float(disc_p * Decimal(max(1, qty_avail))):,.2f}",
                }
            ],
            "establishment": prod.merchant.bus_name if prod.merchant else "Partner Merchant",
            "merchant_phone": (prod.merchant.phone or prod.merchant.hotline or "Verified Store") if prod.merchant else "Verified Store",
            "address": (prod.merchant.address if (prod.merchant and prod.merchant.address) else "Cebu City"),
            "buyer": "Marketplace Discovery Feed",
            "buyer_phone": f"{qty_avail} {u_name} available",
            "buyer_email": "",
            "date": s_rec.safely_anchor_time if (s_rec and s_rec.safely_anchor_time) else "Posted Recently",
            "pickup_deadline": exp_str,
            "total_value": f"₱{float(disc_p):,.2f} / unit",
            "deposit_paid": f"₱{float(dep_20):,.2f} (20% per unit)",
            "remaining_balance": f"₱{float(rem_80):,.2f} (80% per unit)",
            "amount": f"₱{float(disc_p):,.2f} • {qty_avail} {u_name}",
            "status": comm.status or "Active",
            "sort_ts": float(now_dt.timestamp()) - (1000.0 / float(max(1, prod.prod_id))),
        }
        rec["record_json"] = json.dumps(rec)
        combined.append(rec)

    for don in Donation.objects.select_related("merchant", "food_bank", "listing__listing__product").order_by("-donation_id")[:10]:
        p_name = (
            don.listing.listing.product.prod_name
            if (don.listing and don.listing.listing and don.listing.listing.product)
            else "Surplus Donation Bundle"
        )
        d_dt = timezone.localtime(don.created_at) if don.created_at else now_dt
        qty_d = int(don.qty_donated or 1)
        rec = {
            "id": f"DON-{don.donation_id}",
            "record_kind": "Donation",
            "receipt_type": "Community Surplus Donation Handshake Record",
            "gateway_ref": f"DONATION-{don.donation_id}",
            "title": f"{qty_d}x {p_name}",
            "items_breakdown": [
                {
                    "name": p_name,
                    "category": "Community Donation",
                    "qty": qty_d,
                    "unit_price": "₱0.00 (Donated)",
                    "subtotal": f"{qty_d} units",
                }
            ],
            "establishment": don.merchant.bus_name if don.merchant else "Partner Merchant",
            "merchant_phone": (don.merchant.phone or "Verified Store") if don.merchant else "Verified Store",
            "address": (don.merchant.address if (don.merchant and don.merchant.address) else "Cebu City"),
            "buyer": don.food_bank.org_name if don.food_bank else "Open for Food Bank Claim",
            "buyer_phone": (don.food_bank.phone if (don.food_bank and don.food_bank.phone) else "NGO Hub"),
            "buyer_email": "",
            "date": d_dt.strftime("%b %d, %Y • %I:%M %p"),
            "pickup_deadline": "Food Bank Coordination",
            "total_value": f"{qty_d} Donated Units",
            "deposit_paid": "₱0.00 (Charitable Handshake)",
            "remaining_balance": "₱0.00",
            "amount": f"Donation ({qty_d} units)",
            "status": don.status or "Available",
            "sort_ts": float(d_dt.timestamp()),
        }
        rec["record_json"] = json.dumps(rec)
        combined.append(rec)

    combined.sort(key=lambda x: float(x.get("sort_ts") or 0), reverse=True)
    return combined[:limit]


def admin_user_transactions_api(request, role, account_id):
    """
    Unified 360° User Dossier & Transactions endpoint for Consumer, Merchant, and FoodBank modals.
    Returns:
      - behavior (KPI metrics for the modal cards)
      - transactions (all orders / sales / donation handshakes with live dynamic pricing)
      - reviews (ratings & reviews given or received)
      - reports_received (Consumer Reports / No-Show strikes / compliance notices)
      - issues_reported (support tickets & consumer reports filed)
    """
    from listings.models import PerishableProduct, ListingSafety
    from donations.models import Donation, DonationRequest

    role_clean = str(role or "").strip().lower()
    clean_id = int(re.sub(r"\D", "", str(account_id or "0")) or 0)
    gov = get_governance_config()
    strike_limit = int(gov.get("strike_limit", 3))

    behavior = {}
    records = []
    reviews_list = []
    reports_received = []
    issues_reported = []

    if "cons" in role_clean:
        consumer = Consumer.objects.select_related("user").filter(cons_id=clean_id).first()
        orders = list(
            OrderReservation.objects.select_related(
                "consumer__user",
                "listing__listing__product__merchant",
                "listing__listing__product__category",
                "listing__listing__product__unit",
            )
            .filter(consumer_id=clean_id)
            .order_by("-ordered_at", "order_id")[:120]
        )
        order_ids = [o.order_id for o in orders]
        tx_map = {
            t.order_id: t
            for t in PaymentTransaction.objects.filter(order_id__in=order_ids)
        }

        grouped = {}
        for o in orders:
            b_code = str(o.qr_voucher_code or f"OID{100 + o.order_id}").strip().upper().split("-")[0]
            comm = o.listing
            saf = comm.listing if comm else None
            prod = saf.product if saf else None
            merch = prod.merchant if prod else None

            (
                u_price,
                o_price,
                line_tot,
                _,
                dep_paid,
                rem_bal,
                gw_ref,
            ) = resolve_admin_order_live_pricing(o, tx_map)

            cat_name = prod.category.category_name if (prod and prod.category) else "Surplus"
            item_entry = {
                "name": prod.prod_name if prod else f"Item #{o.order_id}",
                "category": cat_name,
                "qty": o.order_qty,
                "unit_price": f"₱{float(u_price):,.2f}",
                "orig_price": f"₱{float(o_price):,.2f}",
                "subtotal": f"₱{float(line_tot):,.2f}",
            }

            if b_code not in grouped:
                grouped[b_code] = {
                    "id": b_code,
                    "gateway_ref": gw_ref or f"XND-{b_code}",
                    "items": [item_entry],
                    "establishment": merch.bus_name if merch else "Partner Merchant",
                    "merchant_phone": (merch.phone or merch.hotline or "Verified Store") if merch else "Verified Store",
                    "address": merch.address if (merch and merch.address) else "Cebu City",
                    "buyer": o.consumer.cons_fullname if o.consumer else "Consumer",
                    "buyer_phone": (o.consumer.phone_num if (o.consumer and o.consumer.phone_num) else "Verified"),
                    "buyer_email": o.consumer.user.user_email if (o.consumer and o.consumer.user) else "",
                    "date": timezone.localtime(o.ordered_at).strftime("%b %d, %Y • %I:%M %p") if o.ordered_at else "N/A",
                    "pickup_deadline": timezone.localtime(saf.estimated_expiry).strftime("%b %d, %Y • %I:%M %p") if (saf and saf.estimated_expiry) else "Standard Window",
                    "total_dec": line_tot,
                    "dep_dec": dep_paid,
                    "rem_dec": rem_bal,
                    "status": o.order_status,
                }
            else:
                g = grouped[b_code]
                g["items"].append(item_entry)
                g["total_dec"] += line_tot
                g["dep_dec"] += dep_paid
                g["rem_dec"] += rem_bal

        completed_vouchers = 0
        no_show_vouchers = 0
        for b_code, g in grouped.items():
            it_list = g["items"]
            title_str = (
                f"{it_list[0]['qty']}x {it_list[0]['name']}"
                if len(it_list) == 1
                else f"{it_list[0]['qty']}x {it_list[0]['name']} +{len(it_list) - 1} more"
            )
            st_val = g["status"] or "Pending"
            if st_val == "Completed":
                completed_vouchers += 1
            elif st_val == "No-Show":
                no_show_vouchers += 1
                reports_received.append({
                    "type": "No-Show Strike",
                    "message": f"Voucher {b_code} ({title_str}) at {g['establishment']} was marked unclaimed past pickup deadline. 20% deposit (₱{float(g['dep_dec']):,.2f}) forfeited.",
                    "date": g["date"],
                })

            records.append({
                "id": b_code,
                "receipt_type": "Consumer Rescue Voucher & Xendit Deposit Receipt",
                "gateway_ref": g["gateway_ref"],
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
                "amount": f"₱{float(g['total_dec']):,.2f} (20% Dep: ₱{float(g['dep_dec']):,.2f} | 80% Cash: ₱{float(g['rem_dec']):,.2f})",
                "status": st_val,
            })

        revs_qs = RatingReview.objects.filter(consumer_id=clean_id).select_related(
            "surplus_listing__listing__product__merchant", "order"
        ).order_by("-review_id")
        avg_r = revs_qs.aggregate(avg=Avg("rating_score"))["avg"] or 0.0
        for r in revs_qs:
            p_prod = (
                r.surplus_listing.listing.product
                if (r.surplus_listing and r.surplus_listing.listing)
                else None
            )
            reviews_list.append({
                "item": p_prod.prod_name if p_prod else "Surplus Food",
                "merchant": (p_prod.merchant.bus_name if (p_prod and p_prod.merchant) else "Merchant"),
                "voucher": (str(r.order.qr_voucher_code).split("-")[0] if r.order else "Verified"),
                "score": int(r.rating_score or 5),
                "comment": r.review_comment or "Great food rescue experience!",
                "date": timezone.localtime(r.created_at).strftime("%b %d, %Y") if getattr(r, "created_at", None) else "Recent",
            })

        if consumer and consumer.user:
            for an in AdminNotification.objects.filter(
                Q(message__icontains=consumer.cons_fullname)
                | Q(message__icontains=consumer.user.user_email)
                | Q(message__icontains=f"CID-{consumer.cons_id}")
            ).order_by("-pk")[:25]:
                dt_fmt = timezone.localtime(an.created_at).strftime("%b %d, %Y • %I:%M %p") if getattr(an, "created_at", None) else "Recent"
                t_low = (an.admin_notif_type or "").lower()
                if "support" in t_low or "report" in t_low:
                    issues_reported.append({
                        "type": "Consumer Report Filed" if "report" in t_low else "Support Ticket",
                        "message": an.message,
                        "date": dt_fmt,
                    })
                elif "compliance" in t_low:
                    reports_received.append({"type": an.admin_notif_type, "message": an.message, "date": dt_fmt})

        saved_kg = float(consumer.total_food_saved or 0) if consumer else 0.0
        behavior = {
            "no_show_strikes": no_show_vouchers,
            "strike_limit": strike_limit,
            "suspension_recommended": no_show_vouchers >= strike_limit,
            "completed_orders": completed_vouchers,
            "total_orders": len(grouped),
            "total_food_saved_kg": f"{saved_kg:,.1f} kg",
            "avg_rating_given": round(float(avg_r), 1),
            "reviews_count": len(reviews_list),
            "reports_received_count": len(reports_received),
            "issues_reported_count": len(issues_reported),
        }

    elif "merch" in role_clean:
        merchant = Merchant.objects.select_related("user", "plan").filter(merch_id=clean_id).first()
        orders = list(
            OrderReservation.objects.select_related(
                "consumer__user",
                "listing__listing__product__merchant",
                "listing__listing__product__category",
                "listing__listing__product__unit",
            )
            .filter(listing__listing__product__merchant_id=clean_id)
            .order_by("-ordered_at", "order_id")[:120]
        )
        order_ids = [o.order_id for o in orders]
        tx_map = {
            t.order_id: t
            for t in PaymentTransaction.objects.filter(order_id__in=order_ids)
        }

        grouped = {}
        for o in orders:
            b_code = str(o.qr_voucher_code or f"OID{100 + o.order_id}").strip().upper().split("-")[0]
            comm = o.listing
            saf = comm.listing if comm else None
            prod = saf.product if saf else None
            merch = prod.merchant if prod else merchant

            (
                u_price,
                o_price,
                line_tot,
                _,
                dep_paid,
                rem_bal,
                gw_ref,
            ) = resolve_admin_order_live_pricing(o, tx_map)

            cat_name = prod.category.category_name if (prod and prod.category) else "Surplus"
            item_entry = {
                "name": prod.prod_name if prod else f"Item #{o.order_id}",
                "category": cat_name,
                "qty": o.order_qty,
                "unit_price": f"₱{float(u_price):,.2f}",
                "orig_price": f"₱{float(o_price):,.2f}",
                "subtotal": f"₱{float(line_tot):,.2f}",
            }

            if b_code not in grouped:
                grouped[b_code] = {
                    "id": b_code,
                    "gateway_ref": gw_ref or f"XND-{b_code}",
                    "items": [item_entry],
                    "establishment": merch.bus_name if merch else "Partner Merchant",
                    "merchant_phone": (merch.phone or merch.hotline or "Verified Store") if merch else "Verified Store",
                    "address": merch.address if (merch and merch.address) else "Cebu City",
                    "buyer": o.consumer.cons_fullname if o.consumer else "Consumer",
                    "buyer_phone": (o.consumer.phone_num if (o.consumer and o.consumer.phone_num) else "Verified"),
                    "buyer_email": o.consumer.user.user_email if (o.consumer and o.consumer.user) else "",
                    "date": timezone.localtime(o.ordered_at).strftime("%b %d, %Y • %I:%M %p") if o.ordered_at else "N/A",
                    "pickup_deadline": timezone.localtime(saf.estimated_expiry).strftime("%b %d, %Y • %I:%M %p") if (saf and saf.estimated_expiry) else "Standard Window",
                    "total_dec": line_tot,
                    "dep_dec": dep_paid,
                    "rem_dec": rem_bal,
                    "status": o.order_status,
                }
            else:
                g = grouped[b_code]
                g["items"].append(item_entry)
                g["total_dec"] += line_tot
                g["dep_dec"] += dep_paid
                g["rem_dec"] += rem_bal

        completed_sales_count = 0
        for b_code, g in grouped.items():
            it_list = g["items"]
            title_str = (
                f"{it_list[0]['qty']}x {it_list[0]['name']}"
                if len(it_list) == 1
                else f"{it_list[0]['qty']}x {it_list[0]['name']} +{len(it_list) - 1} more"
            )
            st_val = g["status"] or "Pending"
            if st_val == "Completed":
                completed_sales_count += 1

            records.append({
                "id": b_code,
                "receipt_type": "Merchant Rescue Order & Split Settlement Receipt",
                "gateway_ref": g["gateway_ref"],
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
                "amount": f"₱{float(g['total_dec']):,.2f} (20% Dep: ₱{float(g['dep_dec']):,.2f})",
                "status": st_val,
            })

        merch_donations = list(
            Donation.objects.select_related("food_bank", "listing__listing__product")
            .filter(merchant_id=clean_id)
            .order_by("-donation_id")[:40]
        )
        for d in merch_donations:
            p_name = (
                d.listing.listing.product.prod_name
                if (d.listing and d.listing.listing and d.listing.listing.product)
                else "Surplus Donation"
            )
            qty_d = int(d.qty_donated or 1)
            d_dt = timezone.localtime(d.created_at).strftime("%b %d, %Y • %I:%M %p") if d.created_at else "Recent"
            records.append({
                "id": f"DON-{d.donation_id}",
                "receipt_type": "Merchant Surplus Donation Handshake Receipt",
                "gateway_ref": f"DONATION-{d.donation_id}",
                "title": f"{qty_d}x {p_name} (Donation)",
                "items_breakdown": [
                    {
                        "name": p_name,
                        "category": "Surplus Donation",
                        "qty": qty_d,
                        "unit_price": "₱0.00 (Donated)",
                        "subtotal": f"{qty_d} units",
                    }
                ],
                "establishment": merchant.bus_name if merchant else "Merchant",
                "merchant_phone": (merchant.phone or "Verified Store") if merchant else "Verified Store",
                "address": (merchant.address or "Cebu City") if merchant else "Cebu City",
                "buyer": d.food_bank.org_name if d.food_bank else "Open / Partner Food Bank",
                "buyer_phone": (d.food_bank.phone if (d.food_bank and d.food_bank.phone) else "NGO Hub"),
                "buyer_email": "",
                "date": d_dt,
                "pickup_deadline": "Food Bank Handshake",
                "total_value": f"{qty_d} Units Donated",
                "deposit_paid": "₱0.00",
                "remaining_balance": "₱0.00",
                "amount": f"{qty_d} units",
                "status": d.status or "Available",
            })

        revs_qs = RatingReview.objects.filter(
            surplus_listing__listing__product__merchant_id=clean_id
        ).select_related("consumer", "surplus_listing__listing__product", "order").order_by("-review_id")
        avg_r = revs_qs.aggregate(avg=Avg("rating_score"))["avg"] or 0.0
        for r in revs_qs:
            p_prod = (
                r.surplus_listing.listing.product
                if (r.surplus_listing and r.surplus_listing.listing)
                else None
            )
            reviews_list.append({
                "consumer": r.consumer.cons_fullname if r.consumer else "Verified Consumer",
                "item": p_prod.prod_name if p_prod else "Surplus Food",
                "voucher": (str(r.order.qr_voucher_code).split("-")[0] if r.order else "Verified"),
                "score": int(r.rating_score or 5),
                "comment": r.review_comment or "Great food quality and pickup experience!",
                "date": timezone.localtime(r.created_at).strftime("%b %d, %Y") if getattr(r, "created_at", None) else "Recent",
            })

        consumer_reports_cnt = 0
        for an in AdminNotification.objects.filter(merchant_id=clean_id).exclude(
            admin_notif_type__startswith="Broadcast"
        ).order_by("-pk")[:40]:
            dt_fmt = timezone.localtime(an.created_at).strftime("%b %d, %Y • %I:%M %p") if getattr(an, "created_at", None) else "Recent"
            t_clean = str(an.admin_notif_type or "").strip()
            t_low = t_clean.lower()
            if "support" in t_low:
                issues_reported.append({"type": "Support Ticket", "message": an.message, "date": dt_fmt})
            elif t_low == "report":
                consumer_reports_cnt += 1
                reports_received.append({"type": "Consumer Report", "message": an.message, "date": dt_fmt})
            elif t_low in ("compliance", "kyb"):
                reports_received.append({"type": "Compliance Notice", "message": an.message, "date": dt_fmt})

        active_listings_cnt = ListingSafety.objects.filter(
            product__merchant_id=clean_id, commerce__status="Active", commerce__qty_available__gt=0
        ).count()

        behavior = {
            "avg_rating": round(float(avg_r), 1),
            "reviews_count": len(reviews_list),
            "active_listings": active_listings_cnt,
            "completed_sales": completed_sales_count,
            "donations_posted": len(merch_donations),
            "wallet_balance": f"₱{float(merchant.wallet_bal or 0):,.2f}" if merchant else "₱0.00",
            "consumer_reports_count": consumer_reports_cnt,
            "reports_received_count": len(reports_received),
            "issues_reported_count": len(issues_reported),
        }

    elif any(k in role_clean for k in ("food", "fb", "ngo")):
        fb = FoodBank.objects.select_related("user").filter(org_id=clean_id).first()
        fb_dons = list(
            Donation.objects.select_related("merchant", "listing__listing__product")
            .filter(food_bank_id=clean_id)
            .order_by("-donation_id")[:60]
        )
        fb_reqs = list(
            DonationRequest.objects.select_related("merchant")
            .filter(food_bank_id=clean_id)
            .order_by("-donation_req_id")[:60]
        )

        success_cnt = 0
        ongoing_cnt = 0

        for d in fb_dons:
            p_name = (
                d.listing.listing.product.prod_name
                if (d.listing and d.listing.listing and d.listing.listing.product)
                else "Surplus Bundle"
            )
            qty_d = int(d.qty_donated or 1)
            st_low = (d.status or "").lower()
            if any(k in st_low for k in ("success", "complet", "deliver", "fulfill")):
                success_cnt += 1
            elif any(k in st_low for k in ("ongoing", "claim", "pend", "active")):
                ongoing_cnt += 1

            d_dt = timezone.localtime(d.created_at).strftime("%b %d, %Y • %I:%M %p") if d.created_at else "Recent"
            records.append({
                "id": f"DON-{d.donation_id}",
                "receipt_type": "Food Bank Claimed Donation Handshake Receipt",
                "gateway_ref": f"FB-DON-{d.donation_id}",
                "title": p_name,
                "items_breakdown": [
                    {
                        "name": p_name,
                        "category": "Surplus Donation",
                        "qty": qty_d,
                        "unit_price": "₱0.00 (Donated)",
                        "subtotal": f"{qty_d} units",
                    }
                ],
                "establishment": d.merchant.bus_name if d.merchant else "Partner Merchant",
                "merchant_phone": (d.merchant.phone or "Verified Store") if d.merchant else "Verified Store",
                "address": (d.merchant.address or "Cebu City") if d.merchant else "Cebu City",
                "buyer": fb.org_name if fb else "Food Bank Hub",
                "buyer_phone": (fb.phone or "Verified NGO") if fb else "Verified NGO",
                "buyer_email": (fb.user.user_email if (fb and fb.user) else ""),
                "date": d_dt,
                "pickup_deadline": "Community Handshake",
                "total_value": f"{qty_d} Units",
                "deposit_paid": "₱0.00",
                "remaining_balance": "₱0.00",
                "amount": f"{qty_d} units",
                "status": d.status or "Claimed",
            })

        for r in fb_reqs:
            b_code = r.batch_code or f"REQ-{r.donation_req_id}"
            qty_r = int(r.qty_needed or 1)
            st_low = (r.status or "").lower()
            if any(k in st_low for k in ("success", "complet", "fulfill")):
                success_cnt += 1
            elif any(k in st_low for k in ("ongoing", "pend", "active")):
                ongoing_cnt += 1

            r_dt = timezone.localtime(r.created_at).strftime("%b %d, %Y • %I:%M %p") if r.created_at else "Recent"
            records.append({
                "id": b_code,
                "receipt_type": "Food Bank Bulk Appeal & Request Record",
                "gateway_ref": b_code,
                "title": r.food_type or "Requested Surplus",
                "items_breakdown": [
                    {
                        "name": r.food_type or "Requested Surplus",
                        "category": "NGO Appeal",
                        "qty": qty_r,
                        "unit_price": "₱0.00 (Appeal)",
                        "subtotal": f"{qty_r} units",
                    }
                ],
                "establishment": r.merchant.bus_name if r.merchant else "Any Partner Merchant",
                "merchant_phone": (r.merchant.phone if (r.merchant and r.merchant.phone) else "Partner Network"),
                "address": (r.merchant.address if (r.merchant and r.merchant.address) else "Cebu City"),
                "buyer": fb.org_name if fb else "Food Bank Hub",
                "buyer_phone": (fb.phone or "Verified NGO") if fb else "Verified NGO",
                "buyer_email": (fb.user.user_email if (fb and fb.user) else ""),
                "date": r_dt,
                "pickup_deadline": r.reason or "Community Relief",
                "total_value": f"{qty_r} Units Requested",
                "deposit_paid": "₱0.00",
                "remaining_balance": "₱0.00",
                "amount": f"{qty_r} units",
                "status": r.status or "Pending",
            })

        total_resc = float(fb.total_food_received or 0) if fb else 0.0
        behavior = {
            "total_rescued_units": f"{total_resc:,.0f} Units",
            "successful_handshakes": success_cnt,
            "ongoing_handshakes": ongoing_cnt,
            "reports_received_count": 0,
            "issues_reported_count": 0,
        }

    return JsonResponse({
        "behavior": behavior,
        "transactions": records,
        "reviews": reviews_list,
        "reports_received": reports_received,
        "issues_reported": issues_reported,
    })


admin_user_dossier_api = admin_user_transactions_api


@csrf_exempt
def update_admin_profile_api(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)

    try:
        data = json.loads(request.body)
        action = data.get('action', 'update_profile')
        admin_user = User.objects.filter(user_role='Admin').first() or User.objects.first()

        if action == 'logout':
            if admin_user:
                log_system_activity("Security", f"Super Administrator ({admin_user.user_email}) logged out of the Enterprise Console.")
            django_logout(request)
            request.session.flush()
            return JsonResponse({'message': 'Admin session terminated.'})

        if not admin_user:
            return JsonResponse({'error': 'Admin account not found.'}, status=404)

        if action == 'update_profile':
            new_email = (data.get('email') or '').strip()
            if not new_email:
                return JsonResponse({'error': 'Email cannot be empty.'}, status=400)
            admin_user.user_email = new_email
            admin_user.save()
            log_system_activity("Admin Settings", f"Administrator updated official system email to {new_email}.")
            return JsonResponse({'message': 'Admin account settings saved.', 'email': new_email})

        elif action == 'change_password':
            current_pw = (data.get('current_password') or '').strip()
            new_pw = (data.get('new_password') or '').strip()
            if len(new_pw) < 6:
                return JsonResponse({'error': 'New password must be at least 6 characters.'}, status=400)

            if current_pw and admin_user.user_pass:
                is_valid = check_password(current_pw, admin_user.user_pass) or (current_pw == admin_user.user_pass)
                if not is_valid:
                    return JsonResponse({'error': 'Current password is incorrect.'}, status=400)

            admin_user.user_pass = make_password(new_pw)
            admin_user.save()
            log_system_activity("Security", "Administrator password was updated via Enterprise Settings.")
            return JsonResponse({'message': 'Administrator password updated successfully!'})

        elif action == 'save_governance':
            gov_kwargs = {
                "strike_limit": data.get('strike_limit', 3),
                "radius_km": data.get('radius_km', 10.0),
                "free_cap": data.get('free_cap', 10),
                "commission_pct": data.get('commission_pct', 10.0),
                "premium_monthly_price": data.get('premium_monthly_price', 149.00),
            }
            sig = inspect.signature(save_governance_config)
            for k, v in {
                "pro_commission_pct": data.get("pro_commission_pct", 4.0),
                "deposit_pct": data.get("deposit_pct", 20.0),
                "min_withdrawal": data.get("min_withdrawal", 100.0),
            }.items():
                if k in sig.parameters:
                    gov_kwargs[k] = v

            updated = save_governance_config(**gov_kwargs)
            log_system_activity(
                "Governance",
                (
                    f"Updated governance: Strike Limit={updated.get('strike_limit', 3)}, "
                    f"Radius={updated.get('radius_km', 10)}km, Free Cap={updated.get('free_cap', 10)}/wk, "
                    f"Basic Commission={updated.get('commission_pct', 10)}%, "
                    f"Pro Price=₱{float(updated.get('premium_monthly_price', 149.00)):,.2f}/mo."
                ),
            )
            return JsonResponse({
                'message': 'All enterprise governance rules and subscription prices are now live across the platform!',
                'config': updated,
            })
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=400)


def get_base_admin_context(active_page):
    admin_user = User.objects.filter(user_role='Admin').first() or User.objects.first()
    return {
        'active_page': active_page,
        'pending_merchants': Merchant.objects.filter(status='Pending').count(),
        'recent_notifications': AdminNotification.objects.all().order_by('-pk')[:10],
        'admin_email': admin_user.user_email if admin_user else 'admin@reserve.org',
        'gov_config': get_governance_config(),
    }


def login(request):
    return render(request, 'login.html')


def admin_dashboard(request):
    context = get_base_admin_context('dashboard')
    gov = context['gov_config']
    active_merchants_count = Merchant.objects.filter(status='Active').count()
    total_consumers = Consumer.objects.count()
    total_foodbanks = FoodBank.objects.count()

    cons_saved = Consumer.objects.aggregate(total=Sum('total_food_saved'))['total'] or Decimal('0.00')
    fb_received = FoodBank.objects.aggregate(total=Sum('total_food_received'))['total'] or Decimal('0.00')
    total_food_rescued_kg = float(cons_saved + fb_received)

    merch_wallets = Merchant.objects.aggregate(total=Sum('wallet_bal'))['total'] or Decimal('0.00')
    order_commissions = PaymentTransaction.objects.filter(order__order_status__in=['Completed', 'No-Show']).aggregate(total=Sum('commission_taken'))['total'] or Decimal('0.00')
    historical_payout_fees = WithdrawalReq.objects.aggregate(total=Sum('fee_deducted'))['total'] or Decimal('0.00')
    pro_merchants_count = Merchant.objects.filter(plan_id=2).count()
    pro_sub_price = Decimal(str(gov.get('premium_monthly_price', 149.00)))
    pro_subscription_rev = Decimal(pro_merchants_count) * pro_sub_price

    total_platform_revenue = float(order_commissions + historical_payout_fees + pro_subscription_rev)
    total_value_rescued = float(merch_wallets) + total_platform_revenue

    total_merch_all = Merchant.objects.count()
    bakeshop_count = Merchant.objects.filter(merch_type__icontains='bake').count()
    market_count = Merchant.objects.filter(merch_type__icontains='market').count()
    other_count = max(0, total_merch_all - bakeshop_count - market_count)

    if total_merch_all > 0:
        bakeshop_pct = round((bakeshop_count / total_merch_all) * 100)
        market_pct = round((market_count / total_merch_all) * 100)
        other_pct = max(0, 100 - bakeshop_pct - market_pct)
    else:
        bakeshop_pct, market_pct, other_pct = 0, 0, 0

    today = datetime.date.today()
    chart_labels, chart_counts = [], []
    for i in range(5, -1, -1):
        month_date = (today.replace(day=1) - datetime.timedelta(days=i * 28)).replace(day=1)
        chart_labels.append(month_date.strftime('%b %Y'))
        chart_counts.append(User.objects.filter(created_at__year=month_date.year, created_at__month=month_date.month).count())

    audit_logs = []
    now_dt = timezone.now()
    for an in AdminNotification.objects.select_related('user', 'merchant').order_by('-pk')[:120]:
        an_dt = timezone.localtime(an.created_at) if getattr(an, 'created_at', None) else now_dt
        raw_type = an.admin_notif_type or 'System'
        t_low = raw_type.lower()
        if any(k in t_low for k in ('security', 'admin settings', 'governance')):
            bucket = 'security'
        elif any(k in t_low for k in ('kyb', 'approved', 'registration', 'compliance', 'support', 'report')):
            bucket = 'kyb'
        elif any(k in t_low for k in ('payout', 'withdrawal')):
            bucket = 'payouts'
        else:
            bucket = 'operations'

        actor_str = an.merchant.bus_name if an.merchant else (an.user.user_email if an.user else 'System Admin')
        audit_logs.append({
            'id': f"LOG-{1000 + an.pk}",
            'category': raw_type[:32],
            'bucket': bucket,
            'actor': actor_str,
            'message': an.message,
            'timestamp': an_dt.strftime('%Y-%m-%d %I:%M %p'),
            'date': an_dt.strftime('%Y-%m-%d'),
            'sort_ts': an_dt.timestamp(),
        })

    for sess in UserSession.objects.select_related('user').order_by('-ses_id')[:40]:
        s_dt = timezone.localtime(sess.ses_login_time) if sess.ses_login_time else now_dt
        u_email = sess.user.user_email if sess.user else 'Unknown'
        u_role = sess.user.user_role if sess.user else 'User'
        audit_logs.append({
            'id': f"AUTH-{sess.ses_id}",
            'category': f"Auth ({u_role})",
            'bucket': 'security',
            'actor': u_email,
            'message': sess.ses_info or f"Authenticated session started for {u_email} ({u_role}).",
            'timestamp': s_dt.strftime('%Y-%m-%d %I:%M %p'),
            'date': s_dt.strftime('%Y-%m-%d'),
            'sort_ts': s_dt.timestamp(),
        })

    audit_logs.sort(key=lambda x: float(x.get('sort_ts') or 0), reverse=True)

    context.update({
        'active_merchants': active_merchants_count,
        'total_consumers': total_consumers,
        'total_foodbanks': total_foodbanks,
        'total_food_rescued_kg': f"{total_food_rescued_kg:,.1f}",
        'total_value_rescued': f"{total_value_rescued:,.2f}",
        'total_platform_revenue': f"{total_platform_revenue:,.2f}",
        'order_commissions_total': f"{float(order_commissions):,.2f}",
        'pro_subscription_rev': f"{float(pro_subscription_rev):,.2f}",
        'pro_merchants_count': pro_merchants_count,
        'bakeshop_pct': bakeshop_pct,
        'market_pct': market_pct,
        'other_pct': other_pct,
        'bakeshop_count': bakeshop_count,
        'market_count': market_count,
        'other_count': other_count,
        'chart_labels_json': json.dumps(chart_labels),
        'chart_counts_json': json.dumps(chart_counts),
        'live_transactions': get_dashboard_recent_marketplace_records(limit=10),
        'system_audit_logs': audit_logs,
    })
    return render(request, 'admin_dashboard.html', context)


def admin_users(request):
    context = get_base_admin_context('users')
    consumers = Consumer.objects.select_related('user').all().order_by('-user__created_at')
    merchants = Merchant.objects.select_related('user').all().order_by('-user__created_at')
    foodbanks = FoodBank.objects.select_related('user').all().order_by('-user__created_at')

    context.update({
        'consumers': consumers,
        'merchants': merchants,
        'foodbanks': foodbanks,
        'total_registered': consumers.count() + merchants.count() + foodbanks.count(),
        'active_accounts': (
            consumers.filter(status='Active').count() +
            merchants.filter(status='Active').count() +
            foodbanks.filter(status='Active').count()
        ),
        'banned_accounts': (
            consumers.exclude(status='Active').count() +
            merchants.exclude(status='Active').count() +
            foodbanks.exclude(status='Active').count()
        ),
    })
    return render(request, 'admin_user_manage.html', context)


def admin_food_listing(request):
    context = get_base_admin_context('food-listing')
    merchant_filter = request.GET.get('merchant', '').replace('MID-', '').strip()
    selected_merch_id = int(merchant_filter) if merchant_filter.isdigit() else None

    from listings.models import PerishableProduct, ListingSafety, ListingCommerce

    now = timezone.now()
    merch_listing_stats = {}

    all_products = list(PerishableProduct.objects.select_related('category', 'unit', 'merchant__user', 'merchant__plan').order_by('-prod_id'))
    safety_records = {s.product_id: s for s in ListingSafety.objects.select_related('commerce').all()}

    listings_list = []
    active_listings_count = 0
    flagged_listings_count = 0
    expired_listings_count = 0

    for prod in all_products:
        m_id = prod.merchant_id
        if m_id not in merch_listing_stats:
            merch_listing_stats[m_id] = {'active': 0, 'flagged_expired': 0, 'total': 0, 'items': []}

        s_rec = safety_records.get(prod.prod_id)
        comm = getattr(s_rec, 'commerce', None) if s_rec else None

        status_val = comm.status if comm else 'Active'
        qty_val = comm.qty_available if comm else 1
        orig_p = float(comm.orig_price) if comm else 0.0
        disc_p = float(comm.current_price or comm.discounted_price) if comm else 0.0

        expiry_dt = s_rec.estimated_expiry if s_rec else None
        if expiry_dt:
            if timezone.is_naive(expiry_dt):
                expiry_dt = timezone.make_aware(expiry_dt)
            if expiry_dt <= now and status_val == 'Active':
                status_val = 'Expired'
            expiry_str = timezone.localtime(expiry_dt).strftime('%b %d, %Y %I:%M %p')
            date_str = timezone.localtime(expiry_dt).strftime('%Y-%m-%d')
        else:
            expiry_str = 'Not Set'
            date_str = datetime.date.today().strftime('%Y-%m-%d')

        is_flagged_or_expired = ('flag' in status_val.lower()) or ('expir' in status_val.lower()) or (qty_val <= 0 and status_val != 'Active')
        category_bucket = 'flagged_expired' if is_flagged_or_expired else 'active'

        merch_listing_stats[m_id]['total'] += 1
        if category_bucket == 'active':
            merch_listing_stats[m_id]['active'] += 1
        else:
            merch_listing_stats[m_id]['flagged_expired'] += 1

        raw_cat = prod.category.category_name if prod.category else ''
        sec_part = str(prod.prod_type or '').split('|')[0].strip()
        if raw_cat in ('Leafy Greens', 'Vegetables', 'Fruits', 'Root Crops'):
            display_cat = raw_cat
        elif sec_part in ('Plain', 'Sweet', 'Egg-Based'):
            display_cat = sec_part
        elif raw_cat == 'Bread':
            display_cat = 'Plain'
        elif raw_cat in ('Pastry', 'Pastries'):
            display_cat = 'Sweet'
        elif raw_cat == 'Cake':
            display_cat = 'Egg-Based'
        else:
            display_cat = raw_cat or 'Plain'

        item_dict = {
            'raw_id': prod.prod_id,
            'id': f"FL-{prod.prod_id}",
            'title': prod.prod_name,
            'description': prod.prod_desc or 'Fresh surplus food item listed for community rescue.',
            'establishment': prod.merchant.bus_name if prod.merchant else 'Verified Partner',
            'merchant_id': m_id,
            'address': prod.merchant.address if prod.merchant and prod.merchant.address else 'Cebu City, Philippines',
            'date': date_str,
            'expiry': expiry_str,
            'temperature': f"{float(s_rec.posted_temp):.1f}°C" if s_rec and s_rec.posted_temp else "28.0°C",
            'quantity': f"{qty_val} {prod.unit.unit_name if prod.unit else 'pcs'}",
            'orig_price': f"₱{orig_p:,.2f}",
            'disc_price': f"₱{disc_p:,.2f}",
            'category': display_cat,
            'status': status_val,
            'bucket': category_bucket,
            'image_url': prod.image_path if str(prod.image_path).startswith('http') else (f"/media/{prod.image_path}" if prod.image_path else ""),
        }
        merch_listing_stats[m_id]['items'].append(item_dict)

        if not selected_merch_id or selected_merch_id == m_id:
            listings_list.append(item_dict)
            if category_bucket == 'active':
                active_listings_count += 1
            else:
                if 'flag' in status_val.lower():
                    flagged_listings_count += 1
                else:
                    expired_listings_count += 1

    merchant_profiles = []
    for m in Merchant.objects.select_related('user', 'plan').all().order_by('-merch_id'):
        m_stats = merch_listing_stats.get(m.merch_id, {'active': 0, 'flagged_expired': 0, 'total': 0, 'items': []})
        reviews_qs = RatingReview.objects.filter(surplus_listing__listing__product__merchant=m)
        rev_count = reviews_qs.count()
        avg_score = reviews_qs.aggregate(avg=Avg('rating_score'))['avg'] or 0.0

        merchant_profiles.append({
            'merch_id': m.merch_id,
            'id_label': f"MID-{m.merch_id}",
            'bus_name': m.bus_name,
            'merch_type': m.merch_type,
            'address': m.address or 'Cebu City, Cebu',
            'contact_person': m.contact_person or 'Owner',
            'phone': m.phone or 'N/A',
            'hotline': m.hotline or 'N/A',
            'email': m.user.user_email,
            'status': m.status,
            'plan_name': m.plan.plan_name if m.plan else 'Basic',
            'wallet_bal': f"₱{float(m.wallet_bal or 0):,.2f}",
            'expiry_date': m.bus_expiry_date.strftime('%Y-%m-%d') if m.bus_expiry_date else 'N/A',
            'permit_url': f"/media/{m.bus_permit_path}" if m.bus_permit_path else '',
            'active_count': m_stats['active'],
            'flagged_expired_count': m_stats['flagged_expired'],
            'total_count': m_stats['total'],
            'avg_rating': f"{float(avg_score):.1f}",
            'review_count': rev_count,
            'items_json': json.dumps(m_stats['items']),
        })

    claimed_count = OrderReservation.objects.filter(order_status='Completed').count()

    context.update({
        'listings': listings_list,
        'merchant_profiles': merchant_profiles,
        'selected_merch_id': selected_merch_id,
        'active_listings_count': active_listings_count,
        'active_stores_count': Merchant.objects.filter(status='Active').count(),
        'claimed_produce_count': claimed_count,
        'flagged_listings_count': flagged_listings_count + expired_listings_count,
    })
    return render(request, 'admin_food_listing.html', context)


def admin_donations(request):
    context = get_base_admin_context('donations')
    fb_filter = request.GET.get('foodbank', '').replace('FB-', '').strip()
    selected_fb_id = int(fb_filter) if fb_filter.isdigit() else None

    from donations.models import Donation, DonationRequest
    from donations.serializers import DonationRequestSerializer, DonationSerializer

    req_qs = DonationRequest.objects.select_related(
        'merchant__user', 'food_bank__user',
        'listing__listing__product__category', 'listing__listing__product__unit',
        'listing__listing__product__merchant__user',
    ).order_by('-donation_req_id')

    if selected_fb_id:
        req_qs = req_qs.filter(food_bank_id=selected_fb_id)

    serialized_reqs = DonationRequestSerializer(req_qs, many=True).data
    grouped_appeals_map = {}
    appeals_list = []

    for r in serialized_reqs:
        b_code = r.get('batch_code') or f"REQ-{r['donation_req_id']}"
        line_item = {
            'donation_req_id': r['donation_req_id'],
            'food_name': r.get('food_name') or r.get('food_type') or 'Surplus Item',
            'food_type': r.get('food_type') or 'Surplus',
            'qty_needed': int(r.get('qty_needed') or 1),
            'qty_available': int(r.get('qty_available') or 0),
            'unit_name': r.get('unit_name') or 'items',
            'expiry_display': r.get('expiry_display') or 'Standard Window',
            'image_url': r.get('image_path') or 'https://images.unsplash.com/photo-1509440159596-0249088772ff?auto=format&fit=crop&w=200&q=80',
            'status': r.get('status') or 'Pending',
        }
        if b_code not in grouped_appeals_map:
            created_raw = r.get('created_at') or ''
            date_only = created_raw[:10] if len(created_raw) >= 10 else datetime.date.today().strftime('%Y-%m-%d')
            grp = {
                'raw_id': r['donation_req_id'],
                'batch_code': b_code,
                'food_bank_id': r.get('food_bank'),
                'food_bank_name': r.get('food_bank_name') or 'Partner Food Bank',
                'food_bank_contact': f"{r.get('food_bank_contact_person') or 'Coordinator'} ({r.get('food_bank_phone') or 'N/A'})",
                'food_bank_email': r.get('food_bank_email') or 'N/A',
                'food_bank_address': r.get('food_bank_address') or 'Cebu City',
                'merchant_id': r.get('merchant'),
                'merchant_name': r.get('merchant_name') or 'Any Partner Merchant',
                'merchant_contact': f"{r.get('merchant_contact_name') or 'Manager'} ({r.get('merchant_contact') or 'N/A'})",
                'merchant_address': r.get('merchant_address') or 'Cebu City',
                'title': line_item['food_name'],
                'total_qty': line_item['qty_needed'],
                'unit_name': line_item['unit_name'],
                'reason': r.get('reason') or 'Community food relief distribution',
                'additional_info': r.get('additional_info') or 'Standard pickup coordination',
                'expiry_display': line_item['expiry_display'],
                'date': date_only,
                'status': r.get('status') or 'Pending',
                'image_url': line_item['image_url'],
                'items_list': [line_item],
            }
            grouped_appeals_map[b_code] = grp
            appeals_list.append(grp)
        else:
            grp = grouped_appeals_map[b_code]
            grp['items_list'].append(line_item)
            grp['total_qty'] += line_item['qty_needed']
            joined = ", ".join(f"{x['qty_needed']}x {x['food_name']}" for x in grp['items_list'])
            grp['title'] = f"{grp['items_list'][0]['food_name']} +{len(grp['items_list']) - 1} more ({joined})"

    for grp in appeals_list:
        grp['items_json'] = json.dumps(grp['items_list'])

    don_qs = Donation.objects.select_related(
        'merchant__user', 'food_bank__user',
        'listing__listing__product__category', 'listing__listing__product__unit',
    ).order_by('-donation_id')

    if selected_fb_id:
        don_qs = don_qs.filter(food_bank_id=selected_fb_id)

    serialized_dons = DonationSerializer(don_qs, many=True).data
    donations_list = []

    for d in serialized_dons:
        created_raw = d.get('created_at') or ''
        date_only = created_raw[:10] if len(created_raw) >= 10 else datetime.date.today().strftime('%Y-%m-%d')
        donations_list.append({
            'raw_id': d['donation_id'],
            'id': f"DON-{d['donation_id']}",
            'food_name': d.get('food_name') or 'Surplus Bundle',
            'food_type': d.get('food_type') or 'Surplus',
            'description': d.get('description') or 'Surplus food donated for community distribution.',
            'qty_donated': int(d.get('qty_donated') or 1),
            'unit_name': d.get('unit_name') or 'items',
            'amount': f"{d.get('qty_donated') or 1} {d.get('unit_name') or 'items'}",
            'merchant_id': d.get('merchant'),
            'merchant_name': d.get('merchant_name') or 'Partner Merchant',
            'merchant_contact': f"{d.get('merchant_contact_name') or 'Manager'} ({d.get('merchant_contact') or 'N/A'})",
            'merchant_email': d.get('merchant_email') or 'N/A',
            'merchant_address': d.get('merchant_address') or 'Cebu City',
            'food_bank_id': d.get('food_bank'),
            'food_bank_name': d.get('food_bank_name') or 'Open / Unclaimed',
            'food_bank_contact': (
                f"{d.get('food_bank_contact_person') or 'Coordinator'} ({d.get('food_bank_phone') or 'N/A'})"
                if d.get('food_bank_name') else 'Awaiting Food Bank Claim'
            ),
            'food_bank_address': d.get('food_bank_address') or 'Cebu City',
            'expiry_display': d.get('expiry_display') or 'Standard Window',
            'is_ttt': bool(d.get('is_ttt')),
            'date': date_only,
            'status': d.get('status') or 'Available',
            'image_url': d.get('image_path') or 'https://images.unsplash.com/photo-1509440159596-0249088772ff?auto=format&fit=crop&w=200&q=80',
        })

    foodbank_profiles = []
    for fb in FoodBank.objects.select_related('user').all().order_by('-org_id'):
        fb_reqs = DonationRequest.objects.filter(food_bank=fb).order_by('-donation_req_id')
        fb_dons = Donation.objects.filter(food_bank=fb).select_related('merchant', 'listing__listing__product').order_by('-donation_id')

        active_req_count = fb_reqs.filter(status__in=['Pending', 'Active', 'Ongoing']).count()
        claimed_don_count = fb_dons.filter(status__in=['Ongoing', 'Claimed', 'Successful', 'Completed']).count()

        fb_history = []
        for d_obj in fb_dons[:8]:
            p_name = d_obj.listing.listing.product.prod_name if (d_obj.listing and d_obj.listing.listing and d_obj.listing.listing.product) else 'Surplus Food'
            fb_history.append({
                'id': f"DON-{d_obj.donation_id}",
                'type': 'Claimed Donation',
                'item': f"{d_obj.qty_donated}x {p_name}",
                'merchant': d_obj.merchant.bus_name if d_obj.merchant else 'Partner Merchant',
                'date': d_obj.created_at.strftime('%Y-%m-%d') if d_obj.created_at else 'Recent',
                'status': d_obj.status,
            })
        for r_obj in fb_reqs[:8]:
            fb_history.append({
                'id': r_obj.batch_code or f"REQ-{r_obj.donation_req_id}",
                'type': 'Appeal / Request',
                'item': f"{r_obj.qty_needed}x {r_obj.food_type}",
                'merchant': r_obj.merchant.bus_name if r_obj.merchant else 'Any Partner Merchant',
                'date': r_obj.created_at.strftime('%Y-%m-%d') if r_obj.created_at else 'Recent',
                'status': r_obj.status,
            })

        foodbank_profiles.append({
            'org_id': fb.org_id,
            'id_label': f"FB-{fb.org_id}",
            'org_name': fb.org_name,
            'address': fb.address or 'Cebu City, Philippines',
            'contact_person': fb.contact_person or 'NGO Coordinator',
            'phone': fb.phone or 'Not Provided',
            'hotline': fb.hotline or 'N/A',
            'email': fb.user.user_email if fb.user else 'N/A',
            'status': fb.status,
            'total_received': f"{float(fb.total_food_received or 0):,.1f}",
            'active_req_count': active_req_count,
            'claimed_don_count': claimed_don_count,
            'permit_url': f"/media/{fb.accreditation_path}" if fb.accreditation_path else '',
            'date': fb.user.created_at.strftime('%Y-%m-%d') if (fb.user and fb.user.created_at) else 'N/A',
            'history_json': json.dumps(fb_history[:12]),
        })

    pending_appeals_count = sum(1 for a in appeals_list if a['status'].lower() in ('pending', 'active'))
    ongoing_count = (
        sum(1 for a in appeals_list if a['status'].lower() == 'ongoing')
        + sum(1 for d in donations_list if d['status'].lower() in ('available', 'active', 'ongoing', 'claimed'))
    )
    flagged_count = (
        sum(1 for a in appeals_list if a['status'].lower() in ('rejected', 'withdrawn', 'cancelled'))
        + sum(1 for d in donations_list if 'flag' in d['status'].lower() or 'cancel' in d['status'].lower() or 'declin' in d['status'].lower())
    )
    fb_received = FoodBank.objects.aggregate(total=Sum('total_food_received'))['total'] or Decimal('0.00')

    context.update({
        'total_hubs': FoodBank.objects.count(),
        'total_donated': int(fb_received) if fb_received > 0 else len(donations_list),
        'pending_review_count': pending_appeals_count,
        'ongoing_drops_count': ongoing_count,
        'flagged_drops_count': flagged_count,
        'appeals': appeals_list,
        'donations_list': donations_list,
        'foodbank_profiles': foodbank_profiles,
        'selected_fb_id': selected_fb_id,
    })
    return render(request, 'admin_donation_hub.html', context)


def admin_payouts(request):
    context = get_base_admin_context('payouts')
    payout_rows = []
    pending_count, approved_count, processing_count = 0, 0, 0

    withdrawals = WithdrawalReq.objects.select_related('merchant__user', 'merchant__plan').order_by('-withdrawal_id')
    for w in withdrawals:
        m = w.merchant
        bal = float(m.wallet_bal or 0)
        amt = float(w.amount or 0)
        fee = float(w.fee_deducted or 0.0)
        net = float(w.net_payout) if w.net_payout is not None else round(max(0.0, amt - fee), 2)
        st = w.withdrawal_status or 'Settled'

        if 'pend' in st.lower():
            pending_count += 1
        elif 'approv' in st.lower() or 'settled' in st.lower() or 'complet' in st.lower():
            approved_count += 1
        else:
            processing_count += 1

        is_pro = m.plan_id == 2 or ('pro' in (m.plan.plan_name or '').lower() if m.plan else False)
        if fee > 0 and amt > 0:
            applied_pct = round((fee / amt) * 100)
            tier_label = f"Basic Tier ({applied_pct}% Payout Fee)"
        else:
            tier_label = "Pro Tier (0% Payout Fee)" if is_pro else "Basic Tier (0% Payout Fee • Settled per Order)"

        payout_rows.append({
            'raw_id': w.withdrawal_id,
            'id': f"PO-{w.withdrawal_id}",
            'is_merchant_wallet': False,
            'merchant_id': f"MID-{m.merch_id}",
            'merchant_name': m.bus_name,
            'contact_person': m.contact_person or 'Owner',
            'merchant_phone': m.phone or 'N/A',
            'merchant_email': m.user.user_email,
            'tier_name': tier_label,
            'channel': w.channel or 'GCash',
            'account_number': w.gcash_num or m.phone or 'Not Provided',
            'account_name': w.account_name or m.contact_person or m.bus_name,
            'date': (w.processed_at or w.created_at).strftime('%Y-%m-%d %I:%M %p') if (w.processed_at or w.created_at) else m.user.created_at.strftime('%Y-%m-%d'),
            'current_balance': f"₱{bal:,.2f}",
            'requested_amount': f"₱{amt:,.2f}",
            'fee_deducted': f"₱{fee:,.2f}",
            'net_payout': f"₱{net:,.2f}",
            'reference_number': w.reference_number or f"XND-{w.channel or 'GCASH'}-{w.withdrawal_id}",
            'status': st,
        })

    context.update({
        'payouts': payout_rows,
        'total_requests': len(payout_rows),
        'pending_approval_count': pending_count,
        'successful_count': approved_count,
        'processing_count': processing_count,
    })
    return render(request, 'admin_payout_requests.html', context)


def admin_announcements(request):
    context = get_base_admin_context('announcements')
    broadcasts_qs = AdminNotification.objects.filter(admin_notif_type__startswith='Broadcast').order_by('-pk')

    broadcasts = []
    for b in broadcasts_qs:
        audience = b.admin_notif_type.replace('Broadcast:', '').strip() or 'All Users (System-wide)'
        parts = (b.message or '').split(' || ', 1)
        title = parts[0] if len(parts) > 1 else 'System Announcement'
        body = parts[1] if len(parts) > 1 else b.message
        b_dt = timezone.localtime(b.created_at) if getattr(b, 'created_at', None) else timezone.now()
        broadcasts.append({
            'id': b.pk,
            'audience': audience,
            'title': title,
            'body': body,
            'created_at_str': b_dt.strftime('%b %d, %Y • %I:%M %p'),
        })

    context['broadcasts'] = broadcasts
    return render(request, 'admin_announcements.html', context)


@csrf_exempt
def admin_portal_action_api(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)

    try:
        data = json.loads(request.body)
        module = str(data.get('module') or '').strip().lower()
        action = str(data.get('action') or '').strip().lower()
        raw_id = data.get('id')

        if module == 'export_all_csv':
            from listings.models import PerishableProduct, ListingSafety
            from donations.models import Donation, DonationRequest

            rows = []
            rows.append(["=== 1. REGISTERED PLATFORM USERS (CONSUMERS, MERCHANTS, FOOD BANKS) ==="])
            rows.append(["Role", "Account ID", "Name / Establishment", "Email", "Phone", "Address", "Status", "Date Registered"])
            for c in Consumer.objects.select_related('user').all().order_by('-cons_id'):
                rows.append(["Consumer", f"CID-{c.cons_id}", c.cons_fullname, c.user.user_email, c.phone_num or "N/A", c.address or "Cebu City", c.status or "Active", c.user.created_at.strftime('%Y-%m-%d') if c.user.created_at else "N/A"])
            for m in Merchant.objects.select_related('user').all().order_by('-merch_id'):
                rows.append([f"Merchant ({m.merch_type})", f"MID-{m.merch_id}", m.bus_name, m.user.user_email, m.phone or m.hotline or "N/A", m.address or "Cebu City", m.status or "Active", m.user.created_at.strftime('%Y-%m-%d') if m.user.created_at else "N/A"])
            for fb in FoodBank.objects.select_related('user').all().order_by('-org_id'):
                rows.append(["Food Bank / NGO", f"FB-{fb.org_id}", fb.org_name, fb.user.user_email, fb.phone or fb.hotline or "N/A", fb.address or "Cebu City", fb.status or "Active", fb.user.created_at.strftime('%Y-%m-%d') if fb.user.created_at else "N/A"])
            rows.append([])

            rows.append(["=== 2. SURPLUS FOOD LISTINGS ==="])
            rows.append(["Listing ID", "Food Item", "Category", "Establishment", "Original Price", "Rescue Price", "Qty Available", "Expiry Window", "Status"])
            safety_map = {s.product_id: s for s in ListingSafety.objects.select_related('commerce').all()}
            for p in PerishableProduct.objects.select_related('category', 'unit', 'merchant').all().order_by('-prod_id'):
                s_rec = safety_map.get(p.prod_id)
                comm = getattr(s_rec, 'commerce', None) if s_rec else None
                exp_str = timezone.localtime(s_rec.estimated_expiry).strftime('%Y-%m-%d %I:%M %p') if (s_rec and s_rec.estimated_expiry) else "Not Set"
                rows.append([
                    f"FL-{p.prod_id}", p.prod_name, p.category.category_name if p.category else "Surplus",
                    p.merchant.bus_name if p.merchant else "Merchant",
                    f"PHP {float(comm.orig_price or 0):.2f}" if comm else "PHP 0.00",
                    f"PHP {float(comm.discounted_price or 0):.2f}" if comm else "PHP 0.00",
                    f"{comm.qty_available if comm else 0} {p.unit.unit_name if p.unit else 'pcs'}",
                    exp_str, comm.status if comm else "Active"
                ])
            rows.append([])

            rows.append(["=== 3. DONATIONS HUB (FOOD BANK APPEALS & MERCHANT POSTED DONATIONS) ==="])
            rows.append(["Record Type", "Ref / Batch ID", "Food Item / Category", "Quantity", "Merchant", "Food Bank Hub", "Reason / Notes", "Status", "Date"])
            for req in DonationRequest.objects.select_related('merchant', 'food_bank').all().order_by('-donation_req_id'):
                rows.append([
                    "Food Bank Appeal", req.batch_code or f"REQ-{req.donation_req_id}", req.food_type,
                    str(req.qty_needed), req.merchant.bus_name if req.merchant else "Any Merchant",
                    req.food_bank.org_name if req.food_bank else "Food Bank",
                    req.reason or "Community distribution", req.status,
                    req.created_at.strftime('%Y-%m-%d') if req.created_at else "N/A"
                ])
            for don in Donation.objects.select_related('merchant', 'food_bank', 'listing__listing__product').all().order_by('-donation_id'):
                p_name = don.listing.listing.product.prod_name if (don.listing and don.listing.listing and don.listing.listing.product) else "Surplus Bundle"
                rows.append([
                    "Posted Donation", f"DON-{don.donation_id}", p_name, str(don.qty_donated),
                    don.merchant.bus_name if don.merchant else "Merchant",
                    don.food_bank.org_name if don.food_bank else "Unclaimed / Open",
                    don.trigger_type or "Manual", don.status,
                    don.created_at.strftime('%Y-%m-%d') if don.created_at else "N/A"
                ])
            rows.append([])

            rows.append(["=== 4. AUTOMATED XENDIT MERCHANT PAYOUT LEDGER ==="])
            rows.append(["Payout ID", "Merchant", "Channel", "Account Number", "Account Name", "Gross Withdrawn", "Fee Deducted", "Net Disbursed", "Xendit Ref #", "Status", "Date"])
            for w in WithdrawalReq.objects.select_related('merchant').all().order_by('-withdrawal_id'):
                rows.append([
                    f"PO-{w.withdrawal_id}", w.merchant.bus_name if w.merchant else "Merchant",
                    w.channel or "GCash", w.gcash_num or "N/A",
                    w.account_name or (w.merchant.contact_person if w.merchant else "Owner"),
                    f"PHP {float(w.amount or 0):.2f}", f"PHP {float(w.fee_deducted or 0):.2f}",
                    f"PHP {float(w.net_payout or w.amount or 0):.2f}",
                    w.reference_number or "Settled", w.withdrawal_status or "Settled",
                    (w.processed_at or w.created_at).strftime('%Y-%m-%d %I:%M %p') if (w.processed_at or w.created_at) else "N/A"
                ])

            return JsonResponse({'rows': rows})

        if module == 'announcement':
            if action == 'create':
                audience = data.get('audience', 'All Users (System-wide)')
                title = data.get('title', '').strip()
                body = data.get('body', '').strip()
                if not title or not body:
                    return JsonResponse({'error': 'Title and message body are required.'}, status=400)

                admin_user = User.objects.filter(user_role='Admin').first() or User.objects.first()
                notif = AdminNotification.objects.create(user=admin_user, admin_notif_type=f"Broadcast:{audience}"[:50], message=f"{title} || {body}")
                return JsonResponse({'message': 'Broadcast published!', 'id': notif.pk})
            elif action == 'delete':
                AdminNotification.objects.filter(pk=raw_id).delete()
                return JsonResponse({'message': 'Broadcast deleted.'})

        elif module in ('user', 'merchant', 'foodbank', 'consumer', 'notify'):
            role_target = str(data.get('role') or module).strip().lower()
            reason_msg = str(data.get('reason') or data.get('message') or data.get('body') or data.get('notice') or '').strip()
            clean_id_str = re.sub(r'\D', '', str(raw_id or ''))
            target_id = int(clean_id_str) if clean_id_str.isdigit() else 0

            if 'food' in role_target or 'ngo' in role_target or 'fb' in str(raw_id or '').lower():
                fb = FoodBank.objects.select_related('user').filter(org_id=target_id).first()
                if not fb:
                    return JsonResponse({'error': 'Food Bank not found.'}, status=404)
                send_compliance_email(fb.user.user_email, "Important Notice from ReServe Admin", f'Hello {fb.org_name},\n\nOfficial Notice from ReServe Admin:\n"{reason_msg}"')
                log_system_activity("Compliance", f"Admin Notice to {fb.org_name}: {reason_msg}", target_user=fb.user)
                return JsonResponse({'message': f'Notification sent to {fb.org_name}.'})
            elif 'cons' in role_target or 'cid' in str(raw_id or '').lower():
                c = Consumer.objects.select_related('user').filter(cons_id=target_id).first()
                if not c:
                    return JsonResponse({'error': 'Consumer not found.'}, status=404)
                send_compliance_email(c.user.user_email, "Official Notice from ReServe Admin", f'Hello {c.cons_fullname},\n\nMessage from ReServe Admin:\n"{reason_msg}"')
                log_system_activity("Compliance", f"Admin Notice to {c.cons_fullname}: {reason_msg}", target_user=c.user)
                return JsonResponse({'message': f'Notification sent to {c.cons_fullname}.'})
            else:
                m = Merchant.objects.select_related('user').filter(merch_id=target_id).first()
                if not m:
                    return JsonResponse({'error': 'Merchant not found.'}, status=404)
                send_compliance_email(m.user.user_email, "Official Compliance Notice - ReServe Admin", f'Hello {m.bus_name},\n\nMessage from ReServe Admin:\n"{reason_msg}"')
                log_system_activity("Compliance", f"Compliance Notice to {m.bus_name}: {reason_msg}", target_user=m.user, merchant=m)
                return JsonResponse({'message': f'Notification sent to {m.bus_name}.'})

        elif module == 'listing':
            from listings.models import PerishableProduct, ListingSafety
            prod = PerishableProduct.objects.select_related('merchant__user').filter(prod_id=raw_id).first()
            if prod:
                if action == 'delete':
                    has_pending = OrderReservation.objects.filter(listing__listing__product=prod, order_status='Pending').exists()
                    if has_pending:
                        return JsonResponse({'error': f"Cannot remove '{prod.prod_name}' because it has active Pending Orders awaiting customer pickup."}, status=400)
                    prod_name = prod.prod_name
                    merch_user = prod.merchant.user if prod.merchant else None
                    prod.delete()
                    log_system_activity("Listing", f"Admin removed food listing '{prod_name}'.", target_user=merch_user)
                    return JsonResponse({'message': 'Listing removed.'})
                elif action == 'flag':
                    s_rec = ListingSafety.objects.select_related('commerce').filter(product=prod).first()
                    if s_rec and hasattr(s_rec, 'commerce'):
                        s_rec.commerce.status = 'Flagged'
                        s_rec.commerce.save()
                    log_system_activity("Compliance", f"Your food listing '{prod.prod_name}' was flagged by Admin for review.", target_user=prod.merchant.user if prod.merchant else None, merchant=prod.merchant)
                    return JsonResponse({'message': 'Listing flagged.'})
            return JsonResponse({'message': 'Listing status updated.'})

        elif module == 'donation':
            from donations.models import Donation, DonationRequest

            record_type = data.get('record_type', 'donation')
            batch_code = data.get('batch_code', '')

            if record_type == 'foodbank_org' or data.get('is_fb_org', False):
                fb = FoodBank.objects.filter(org_id=raw_id).first()
                if fb:
                    fb.status = 'Active' if action == 'approve' else 'Rejected'
                    fb.save()
                    return JsonResponse({'message': f'Food Bank {fb.org_name} status updated to {fb.status}.'})

            if record_type == 'appeal':
                qs = DonationRequest.objects.select_related('merchant__user', 'food_bank__user', 'listing')
                target_rows = list(qs.filter(batch_code=batch_code)) if batch_code else list(qs.filter(donation_req_id=raw_id))
                if not target_rows:
                    return JsonResponse({'error': 'Appeal record not found.'}, status=404)

                fb_obj = target_rows[0].food_bank
                m_obj = target_rows[0].merchant
                b_label = target_rows[0].batch_code or f"REQ-{target_rows[0].donation_req_id}"

                if action == 'approve':
                    for r in target_rows:
                        if r.listing and r.status in ('Pending', 'Active'):
                            avail = int(r.listing.qty_available or 0)
                            deduct = min(avail, int(r.qty_needed or 1))
                            if deduct > 0:
                                r.listing.qty_available = max(0, avail - deduct)
                                r.listing.save()
                        r.status = 'Ongoing'
                        r.updated_at = timezone.now()
                        r.save()
                    log_system_activity("Donation", f"Admin approved Food Bank appeal {b_label} for {fb_obj.org_name if fb_obj else 'Food Bank'} (Status: Ongoing).", target_user=fb_obj.user if fb_obj else None, merchant=m_obj)
                    return JsonResponse({'message': f'Appeal {b_label} approved and set to Ongoing.'})

                elif action in ('deliver', 'complete', 'successful'):
                    total_u = Decimal('0.00')
                    for r in target_rows:
                        r.status = 'Successful'
                        r.updated_at = timezone.now()
                        r.save()
                        total_u += Decimal(str(r.qty_needed or 1))
                    if fb_obj:
                        fb_obj.total_food_received = Decimal(str(fb_obj.total_food_received or 0)) + total_u
                        fb_obj.save(update_fields=['total_food_received'])
                    log_system_activity("Donation", f"Admin marked Food Bank appeal {b_label} as Successful ({float(total_u):,.0f} units rescued).", target_user=fb_obj.user if fb_obj else None, merchant=m_obj)
                    return JsonResponse({'message': f'Appeal {b_label} marked as Successful!'})

                elif action in ('reject', 'cancel', 'delete'):
                    if action == 'delete':
                        for r in target_rows:
                            r.delete()
                        return JsonResponse({'message': f'Appeal {b_label} removed.'})
                    for r in target_rows:
                        r.status = 'Rejected'
                        r.updated_at = timezone.now()
                        r.save()
                    log_system_activity("Donation", f"Admin rejected Food Bank appeal {b_label}.", target_user=fb_obj.user if fb_obj else None, merchant=m_obj)
                    return JsonResponse({'message': f'Appeal {b_label} marked as Rejected.'})

            else:
                don = Donation.objects.select_related('merchant__user', 'food_bank__user', 'listing').filter(donation_id=raw_id).first()
                if not don:
                    return JsonResponse({'error': 'Donation record not found.'}, status=404)

                if action in ('deliver', 'complete', 'successful'):
                    don.status = 'Successful'
                    don.updated_at = timezone.now()
                    don.save()
                    if don.food_bank:
                        don.food_bank.total_food_received = Decimal(str(don.food_bank.total_food_received or 0)) + Decimal(str(don.qty_donated or 1))
                        don.food_bank.save(update_fields=['total_food_received'])
                    log_system_activity("Donation", f"Admin marked Donation DON-{don.donation_id} as Successful.", target_user=don.merchant.user if don.merchant else None, merchant=don.merchant)
                    return JsonResponse({'message': f'Donation DON-{don.donation_id} marked as Successful.'})

                elif action == 'flag':
                    don.status = 'Flagged'
                    don.updated_at = timezone.now()
                    don.save()
                    log_system_activity("Compliance", f"Admin flagged Donation DON-{don.donation_id} from {don.merchant.bus_name if don.merchant else 'Merchant'}.", target_user=don.merchant.user if don.merchant else None, merchant=don.merchant)
                    return JsonResponse({'message': f'Donation DON-{don.donation_id} flagged for review.'})

                elif action in ('cancel', 'reject', 'delete'):
                    if don.status in ('Active', 'Available') and don.listing:
                        don.listing.qty_available = int(don.listing.qty_available or 0) + int(don.qty_donated or 0)
                        if don.listing.status == 'Donated':
                            don.listing.status = 'Active'
                        don.listing.save()
                    if action == 'delete':
                        don.delete()
                        return JsonResponse({'message': 'Donation record deleted and stock restored.'})
                    don.status = 'Cancelled'
                    don.updated_at = timezone.now()
                    don.save()
                    return JsonResponse({'message': f'Donation DON-{don.donation_id} cancelled.'})

            return JsonResponse({'message': 'Donation status updated.'})

    except Exception as e:
        return JsonResponse({'error': str(e)}, status=400)

    return JsonResponse({'error': 'Unknown module or action'}, status=400)


def sync_table_sequence(table_name, pk_col):
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT setval(pg_get_serial_sequence('{table_name}', '{pk_col}'), "
                f"COALESCE((SELECT MAX({pk_col}) FROM {table_name}), 1), true);"
            )
    except Exception as e:
        print(f"Sequence sync notice ({table_name}):", e)