import json
import os
from decimal import Decimal
from django.conf import settings
from accounts.models import SubscriptionPlan

GOV_FILE_PATH = os.path.join(settings.BASE_DIR, "governance_config.json")

DEFAULT_GOVERNANCE = {
    "strike_limit": 3,
    "radius_km": 10.0,
    "free_cap": 10,
    "commitment_fee_pct": 20.0,
    "commission_pct": 10.0,
    "pro_commission_pct": 4.0,
    "premium_monthly_price": 149.00,
    "min_withdrawal_php": 100.00,
}


def get_governance_config():
    config = dict(DEFAULT_GOVERNANCE)
    if os.path.exists(GOV_FILE_PATH):
        try:
            with open(GOV_FILE_PATH, "r", encoding="utf-8") as f:
                saved = json.load(f)
                if isinstance(saved, dict):
                    # Auto-migrate legacy 399 default to the new 149 Pro subscription price
                    if float(saved.get("premium_monthly_price", 149.0)) == 399.0:
                        saved["premium_monthly_price"] = 149.00
                    config.update(saved)
        except Exception:
            pass

    return {
        "strike_limit": max(1, int(config.get("strike_limit", 3))),
        "radius_km": max(1.0, float(config.get("radius_km", 10.0))),
        "free_cap": max(1, int(config.get("free_cap", 10))),
        "commitment_fee_pct": max(1.0, float(config.get("commitment_fee_pct", 20.0))),
        "commission_pct": max(0.0, float(config.get("commission_pct", 10.0))),
        "pro_commission_pct": max(0.0, float(config.get("pro_commission_pct", 4.0))),
        "premium_monthly_price": max(0.0, float(config.get("premium_monthly_price", 149.00))),
        "min_withdrawal_php": max(1.0, float(config.get("min_withdrawal_php", 100.00))),
    }


def save_governance_config(
    strike_limit=3,
    radius_km=10.0,
    free_cap=10,
    commission_pct=10.0,
    premium_monthly_price=149.00,
    pro_commission_pct=4.0,
    commitment_fee_pct=20.0,
    min_withdrawal_php=100.00,
):
    cleaned = {
        "strike_limit": max(1, int(float(strike_limit))),
        "radius_km": round(max(1.0, float(radius_km)), 1),
        "free_cap": max(1, int(float(free_cap))),
        "commitment_fee_pct": round(max(1.0, float(commitment_fee_pct)), 2),
        "commission_pct": round(max(0.0, float(commission_pct)), 2),
        "pro_commission_pct": round(max(0.0, float(pro_commission_pct)), 2),
        "premium_monthly_price": round(max(0.0, float(premium_monthly_price)), 2),
        "min_withdrawal_php": round(max(1.0, float(min_withdrawal_php)), 2),
    }

    with open(GOV_FILE_PATH, "w", encoding="utf-8") as f:
        json.dump(cleaned, f, indent=2)

    try:
        SubscriptionPlan.objects.update_or_create(
            plan_id=1,
            defaults={
                "plan_name": f"Basic Tier ({cleaned['free_cap']}/wk • {cleaned['commission_pct']:.0f}% Fee)",
                "commission_rate": Decimal(str(cleaned["commission_pct"])),
            },
        )
        SubscriptionPlan.objects.update_or_create(
            plan_id=2,
            defaults={
                "plan_name": f"Pro Tier (₱{cleaned['premium_monthly_price']:,.2f}/mo • {cleaned['pro_commission_pct']:.0f}% Fee)",
                "commission_rate": Decimal(str(cleaned["pro_commission_pct"])),
            },
        )
    except Exception as e:
        print("SubscriptionPlan sync notice:", e)

    return cleaned