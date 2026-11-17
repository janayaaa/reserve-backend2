from django.utils import timezone
from rest_framework import serializers
from accounts.models import Location
from .models import Donation, DonationRequest


def merchant_primary_location(merchant):
    if not merchant or not merchant.user_id:
        return "Cebu City, Philippines", 10.3157, 123.8854
    loc = Location.objects.filter(user_id=merchant.user_id).order_by('-is_primary', '-loc_id').first()
    addr = merchant.address or (loc.address_name if loc else "Cebu City, Philippines")
    lat = float(loc.latitude) if loc else 10.3157
    lng = float(loc.longitude) if loc else 123.8854
    return addr, lat, lng


def merchant_contact_details(merchant):
    if not merchant:
        return "Not Provided", "Store Manager", ""
    phone = merchant.phone or merchant.hotline or ""
    name = merchant.contact_person or merchant.bus_name or "Store Manager"
    email = merchant.user.user_email if merchant.user else ""
    if not phone:
        contacts = list(merchant.contacts.all())
        if contacts:
            phone = contacts[0].contact_phone
            name = contacts[0].contact_fullname or name
            email = contacts[0].contact_email or email
    return phone or "Not Provided", name, email


def foodbank_contact_details(fb):
    if not fb:
        return "Partner Food Bank", "Coordinator", "Not Provided", "N/A", "", "Cebu City"
    return (
        fb.org_name,
        fb.contact_person or "NGO Coordinator",
        fb.phone or fb.hotline or "Not Provided",
        fb.hotline or "N/A",
        fb.user.user_email if fb.user else "",
        fb.address or "Cebu City, Philippines",
    )


def listing_parts(commerce):
    safety = commerce.listing if commerce else None
    product = safety.product if safety else None
    return commerce, safety, product


def donation_parts(donation):
    return listing_parts(donation.listing)


def commerce_expires_at(commerce):
    commerce, safety, _ = listing_parts(commerce)
    times = []
    if safety and safety.estimated_expiry:
        times.append(safety.estimated_expiry)
    if commerce and commerce.pickup_deadline:
        times.append(commerce.pickup_deadline)
    return min(times) if times else None


def donation_expires_at(donation):
    return commerce_expires_at(donation.listing)


def format_expiry_strings(dt_val):
    if not dt_val:
        return "Not Set", "Not Set", "Standard Window"
    local_dt = timezone.localtime(dt_val) if timezone.is_aware(dt_val) else dt_val
    d_str = local_dt.strftime("%b %d, %Y")
    t_str = local_dt.strftime("%I:%M %p")
    return d_str, t_str, f"{d_str} • {t_str}"


def donation_is_ttt(donation):
    trigger = (donation.trigger_type or '').lower()
    return trigger == 'system' or 'ttt' in trigger


def donation_food_name(donation):
    _, _, product = donation_parts(donation)
    return product.prod_name if product else "Surplus Bundle"


def donation_food_type(donation):
    _, _, product = donation_parts(donation)
    return product.category.category_name if (product and product.category) else "General Supplies"


def donation_unit(donation):
    _, _, product = donation_parts(donation)
    return product.unit.unit_name if (product and product.unit) else "pcs"


class DonationRequestSerializer(serializers.ModelSerializer):
    voucher_code = serializers.SerializerMethodField()
    qr_payload = serializers.SerializerMethodField()

    merchant_name = serializers.SerializerMethodField()
    merchant_address = serializers.SerializerMethodField()
    merchant_contact = serializers.SerializerMethodField()
    merchant_contact_name = serializers.SerializerMethodField()
    merchant_email = serializers.SerializerMethodField()
    merchant_lat = serializers.SerializerMethodField()
    merchant_lng = serializers.SerializerMethodField()

    food_bank_name = serializers.SerializerMethodField()
    food_bank_contact_person = serializers.SerializerMethodField()
    food_bank_phone = serializers.SerializerMethodField()
    food_bank_hotline = serializers.SerializerMethodField()
    food_bank_email = serializers.SerializerMethodField()
    food_bank_address = serializers.SerializerMethodField()

    food_name = serializers.SerializerMethodField()
    unit_name = serializers.SerializerMethodField()
    qty_available = serializers.SerializerMethodField()
    image_path = serializers.SerializerMethodField()
    expires_at = serializers.SerializerMethodField()
    expiry_date = serializers.SerializerMethodField()
    expiry_time = serializers.SerializerMethodField()
    expiry_display = serializers.SerializerMethodField()

    class Meta:
        model = DonationRequest
        fields = [
            'donation_req_id',
            'batch_code',
            'voucher_code',
            'qr_payload',
            'food_type',
            'food_name',
            'qty_needed',
            'qty_available',
            'unit_name',
            'reason',
            'additional_info',
            'status',
            'created_at',
            'updated_at',
            'merchant',
            'merchant_name',
            'merchant_address',
            'merchant_contact',
            'merchant_contact_name',
            'merchant_email',
            'merchant_lat',
            'merchant_lng',
            'food_bank',
            'food_bank_name',
            'food_bank_contact_person',
            'food_bank_phone',
            'food_bank_hotline',
            'food_bank_email',
            'food_bank_address',
            'listing',
            'image_path',
            'expires_at',
            'expiry_date',
            'expiry_time',
            'expiry_display',
        ]
        read_only_fields = [
            'donation_req_id',
            'created_at',
            'updated_at',
        ]

    def get_voucher_code(self, obj):
        return obj.batch_code or f"REQ-{obj.donation_req_id}"

    def get_qr_payload(self, obj):
        code = obj.batch_code or f"REQ-{obj.donation_req_id}"
        return f"RESERVE:{code}"

    def _resolve_merchant(self, obj):
        if obj.merchant:
            return obj.merchant
        _, _, prod = listing_parts(obj.listing)
        return prod.merchant if prod else None

    def get_merchant_name(self, obj):
        m = self._resolve_merchant(obj)
        return m.bus_name if m else "Any Partner Merchant"

    def get_merchant_address(self, obj):
        m = self._resolve_merchant(obj)
        return merchant_primary_location(m)[0] if m else "Cebu City"

    def get_merchant_lat(self, obj):
        m = self._resolve_merchant(obj)
        return merchant_primary_location(m)[1] if m else 10.3157

    def get_merchant_lng(self, obj):
        m = self._resolve_merchant(obj)
        return merchant_primary_location(m)[2] if m else 123.8854

    def get_merchant_contact(self, obj):
        m = self._resolve_merchant(obj)
        return merchant_contact_details(m)[0] if m else "Not Provided"

    def get_merchant_contact_name(self, obj):
        m = self._resolve_merchant(obj)
        return merchant_contact_details(m)[1] if m else ""

    def get_merchant_email(self, obj):
        m = self._resolve_merchant(obj)
        return merchant_contact_details(m)[2] if m else ""

    def get_food_bank_name(self, obj):
        return foodbank_contact_details(obj.food_bank)[0]

    def get_food_bank_contact_person(self, obj):
        return foodbank_contact_details(obj.food_bank)[1]

    def get_food_bank_phone(self, obj):
        return foodbank_contact_details(obj.food_bank)[2]

    def get_food_bank_hotline(self, obj):
        return foodbank_contact_details(obj.food_bank)[3]

    def get_food_bank_email(self, obj):
        return foodbank_contact_details(obj.food_bank)[4]

    def get_food_bank_address(self, obj):
        return foodbank_contact_details(obj.food_bank)[5]

    def get_food_name(self, obj):
        _, _, prod = listing_parts(obj.listing)
        return prod.prod_name if prod else obj.food_type

    def get_unit_name(self, obj):
        _, _, prod = listing_parts(obj.listing)
        return prod.unit.unit_name if (prod and prod.unit) else "items"

    def get_qty_available(self, obj):
        if obj.listing:
            return int(obj.listing.qty_available or 0)
        return obj.qty_needed

    def get_image_path(self, obj):
        _, _, prod = listing_parts(obj.listing)
        return prod.image_path if prod else None

    def get_expires_at(self, obj):
        exp = commerce_expires_at(obj.listing)
        return exp.isoformat() if exp else None

    def get_expiry_date(self, obj):
        exp = commerce_expires_at(obj.listing)
        return format_expiry_strings(exp)[0]

    def get_expiry_time(self, obj):
        exp = commerce_expires_at(obj.listing)
        return format_expiry_strings(exp)[1]

    def get_expiry_display(self, obj):
        exp = commerce_expires_at(obj.listing)
        return format_expiry_strings(exp)[2]


class DonationSerializer(serializers.ModelSerializer):
    voucher_code = serializers.SerializerMethodField()
    qr_payload = serializers.SerializerMethodField()
    food_name = serializers.SerializerMethodField()
    food_type = serializers.SerializerMethodField()
    description = serializers.SerializerMethodField()
    unit_name = serializers.SerializerMethodField()
    image_path = serializers.SerializerMethodField()
    expires_at = serializers.SerializerMethodField()
    expiry_date = serializers.SerializerMethodField()
    expiry_time = serializers.SerializerMethodField()
    expiry_display = serializers.SerializerMethodField()
    is_ttt = serializers.SerializerMethodField()

    merchant_name = serializers.CharField(source='merchant.bus_name', read_only=True)
    merchant_address = serializers.SerializerMethodField()
    merchant_contact = serializers.SerializerMethodField()
    merchant_contact_name = serializers.SerializerMethodField()
    merchant_email = serializers.SerializerMethodField()
    merchant_lat = serializers.SerializerMethodField()
    merchant_lng = serializers.SerializerMethodField()

    food_bank_name = serializers.SerializerMethodField()
    food_bank_contact_person = serializers.SerializerMethodField()
    food_bank_phone = serializers.SerializerMethodField()
    food_bank_email = serializers.SerializerMethodField()
    food_bank_address = serializers.SerializerMethodField()

    class Meta:
        model = Donation
        fields = [
            'donation_id',
            'voucher_code',
            'qr_payload',
            'trigger_type',
            'is_ttt',
            'qty_donated',
            'unit_name',
            'status',
            'created_at',
            'updated_at',
            'merchant',
            'merchant_name',
            'merchant_address',
            'merchant_contact',
            'merchant_contact_name',
            'merchant_email',
            'merchant_lat',
            'merchant_lng',
            'food_bank',
            'food_bank_name',
            'food_bank_contact_person',
            'food_bank_phone',
            'food_bank_email',
            'food_bank_address',
            'listing',
            'food_name',
            'food_type',
            'description',
            'image_path',
            'expires_at',
            'expiry_date',
            'expiry_time',
            'expiry_display',
        ]

    def get_voucher_code(self, obj):
        return f"DON-{obj.donation_id}"

    def get_qr_payload(self, obj):
        return f"RESERVE:DON-{obj.donation_id}"

    def get_food_name(self, obj):
        return donation_food_name(obj)

    def get_food_type(self, obj):
        return donation_food_type(obj)

    def get_description(self, obj):
        _, _, product = donation_parts(obj)
        return product.prod_desc if (product and product.prod_desc) else "Fresh surplus food donated for community distribution."

    def get_unit_name(self, obj):
        return donation_unit(obj)

    def get_image_path(self, obj):
        _, _, product = donation_parts(obj)
        return product.image_path if product else None

    def get_expires_at(self, obj):
        expires = donation_expires_at(obj)
        return expires.isoformat() if expires else None

    def get_expiry_date(self, obj):
        expires = donation_expires_at(obj)
        return format_expiry_strings(expires)[0]

    def get_expiry_time(self, obj):
        expires = donation_expires_at(obj)
        return format_expiry_strings(expires)[1]

    def get_expiry_display(self, obj):
        expires = donation_expires_at(obj)
        return format_expiry_strings(expires)[2]

    def get_is_ttt(self, obj):
        return donation_is_ttt(obj)

    def get_merchant_address(self, obj):
        return merchant_primary_location(obj.merchant)[0]

    def get_merchant_lat(self, obj):
        return merchant_primary_location(obj.merchant)[1]

    def get_merchant_lng(self, obj):
        return merchant_primary_location(obj.merchant)[2]

    def get_merchant_contact(self, obj):
        return merchant_contact_details(obj.merchant)[0]

    def get_merchant_contact_name(self, obj):
        return merchant_contact_details(obj.merchant)[1]

    def get_merchant_email(self, obj):
        return merchant_contact_details(obj.merchant)[2]

    def get_food_bank_name(self, obj):
        return obj.food_bank.org_name if obj.food_bank else None

    def get_food_bank_contact_person(self, obj):
        return foodbank_contact_details(obj.food_bank)[1] if obj.food_bank else None

    def get_food_bank_phone(self, obj):
        return foodbank_contact_details(obj.food_bank)[2] if obj.food_bank else None

    def get_food_bank_email(self, obj):
        return foodbank_contact_details(obj.food_bank)[4] if obj.food_bank else None

    def get_food_bank_address(self, obj):
        return foodbank_contact_details(obj.food_bank)[5] if obj.food_bank else None