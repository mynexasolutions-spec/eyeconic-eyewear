"""
shipping.py — iThink Logistics courier integration.

Credentials and pickup/return warehouse IDs are stored in store_settings
(Admin > Settings > iThink Logistics), the same pattern used for Razorpay.
Docs: https://docs.ithinklogistics.com/doc-add-order/3
"""
import requests
from helpers import get_cached_store_settings

BASE_URL = "https://my.ithinklogistics.com/api_v3"


def _settings():
    return get_cached_store_settings()


def infer_order_status(current_status):
    """Conservative mapping from an iThink `current_status` string to our order.status
    enum. Returns None when there's no safe/obvious mapping — callers should leave the
    order status untouched in that case (e.g. RTO / exception statuses need a human)."""
    if not current_status:
        return None
    s = current_status.strip().lower()
    if "undeliver" in s or "non deliver" in s or "rto" in s or "cancel" in s:
        return None
    if "out for delivery" in s:
        return "shipped"
    if "deliver" in s:
        return "delivered"
    if any(k in s for k in ("in transit", "manifested", "picked up", "pickup", "dispatch", "shipped")):
        return "shipped"
    return None


def tracking_url_for(awb_number):
    """Best-effort tracking URL for an AWB, used to backfill orders whose stored
    tracking_url is empty (e.g. shipments created before this fallback existed)."""
    if not awb_number:
        return ""
    return f"https://ithinklogistics.co.in/postship/tracking/{awb_number}"


def is_configured():
    s = _settings()
    return bool(s.get("ithink_access_token", "").strip() and s.get("ithink_secret_key", "").strip())


def _post(path, payload):
    s = _settings()
    payload = dict(payload)
    payload["access_token"] = s.get("ithink_access_token", "").strip()
    payload["secret_key"] = s.get("ithink_secret_key", "").strip()
    resp = requests.post(f"{BASE_URL}/{path}", json={"data": payload}, timeout=25)
    resp.raise_for_status()
    return resp.json()


def create_shipment(order, items, shipping_address, weight=None):
    """
    order: dict — a row from the `orders` table.
    items: list[dict] — rows from `order_items` (needs quantity, unit_price,
           and product_name_snapshot or product_name / sku).
    shipping_address: dict — parsed shipping_address_json
           (first_name, last_name, phone, address_line1, address_line2,
            city, state, pincode, country).
    weight: optional per-order override (kg, string or number). Falls back to
            the store-wide default in Admin > Settings when not given.

    Returns (ok, message, info) where info = {"awb_number", "courier", "tracking_url"} on success.
    """
    if not is_configured():
        return False, "iThink Logistics is not configured. Add your API credentials in Admin > Settings first.", None

    s = _settings()
    pickup_id = s.get("ithink_pickup_address_id", "").strip()
    return_id = s.get("ithink_return_address_id", "").strip() or pickup_id
    if not pickup_id:
        return False, "Pickup Address ID is not set in Admin > Settings.", None

    name = f"{shipping_address.get('first_name', '')} {shipping_address.get('last_name', '')}".strip()
    name = name or order.get("customer_name") or "Customer"

    products = []
    for it in items:
        products.append({
            "product_name": (it.get("product_name_snapshot") or it.get("product_name") or "Item")[:100],
            "product_sku": it.get("sku") or "",
            "product_quantity": str(int(it.get("quantity") or 1)),
            "product_price": str(float(it.get("unit_price") or 0)),
            "product_tax_rate": "0",
            "product_hsn_code": "",
            "product_discount": "0",
            "product_img_url": "",
        })

    order_date = ""
    created_at = order.get("created_at")
    if created_at is not None and hasattr(created_at, "strftime"):
        order_date = created_at.strftime("%d-%m-%Y")

    is_cod = (order.get("payment_method") or "cod").lower() == "cod"
    weight = str(weight).strip() if weight else ""
    weight = weight or s.get("ithink_default_weight_kg", "").strip() or "0.5"
    phone = shipping_address.get("phone") or order.get("customer_phone") or ""

    shipment = {
        "waybill": "",
        "order": order.get("order_number") or str(order.get("id", ""))[:12],
        "sub_order": "",
        "order_date": order_date,
        "total_amount": str(order.get("total_amount") or 0),
        "name": name,
        "company_name": "",
        "add": shipping_address.get("address_line1", ""),
        "add2": shipping_address.get("address_line2", ""),
        "add3": "",
        "pin": shipping_address.get("pincode", ""),
        "city": shipping_address.get("city", ""),
        "state": shipping_address.get("state", ""),
        "country": shipping_address.get("country") or "India",
        "phone": phone,
        "alt_phone": phone,
        "email": order.get("customer_email") or "",
        "is_billing_same_as_shipping": "yes",
        "products": products,
        "shipment_length": "10",
        "shipment_width": "10",
        "shipment_height": "10",
        "weight": weight,
        "shipping_charges": str(order.get("shipping_amount") or 0),
        "giftwrap_charges": "0",
        "transaction_charges": "0",
        "total_discount": str(order.get("discount_amount") or 0),
        "first_attemp_discount": "0",
        "cod_charges": "0",
        "advance_amount": "0",
        "payment_mode": "cod" if is_cod else "Prepaid",
        "cod_amount": str(order.get("total_amount") or 0) if is_cod else "0",
        "return_address_id": return_id,
        "reseller_name": "",
        "eway_bill_number": "",
        "gst_number": "",
        "what3words": "",
    }

    payload = {
        "shipments": [shipment],
        "pickup_address_id": pickup_id,
        "s_type": s.get("ithink_service_type", "").strip() or "surface",
    }
    partner = s.get("ithink_logistics_partner", "").strip()
    if partner:
        payload["logistics"] = partner

    try:
        result = _post("order/add.json", payload)
    except Exception as e:
        return False, f"Could not reach iThink Logistics: {e}", None

    entry = (result.get("data") or {}).get("1") or {}
    if entry.get("status") == "Success" and entry.get("waybill"):
        return True, "Shipment created.", {
            "awb_number": entry.get("waybill", ""),
            "courier": entry.get("logistic_name", ""),
            "tracking_url": entry.get("tracking_url", ""),
        }
    error_msg = entry.get("remark") or result.get("html_message") or "Shipment creation failed."
    return False, error_msg, None


def track_shipment(awb_number):
    """Returns (ok, message, info) where info is iThink's raw tracking entry for this AWB."""
    if not is_configured():
        return False, "iThink Logistics is not configured.", None
    if not awb_number:
        return False, "No AWB number to track.", None
    try:
        result = _post("order/track.json", {"awb_number_list": str(awb_number)})
    except Exception as e:
        return False, f"Could not reach iThink Logistics: {e}", None
    entry = (result.get("data") or {}).get(str(awb_number))
    if not entry:
        return False, "No tracking data found for this AWB yet.", None
    return True, "OK", entry


def cancel_shipment(awb_number):
    """Returns (ok, message, info)."""
    if not is_configured():
        return False, "iThink Logistics is not configured.", None
    if not awb_number:
        return False, "No AWB number to cancel.", None
    try:
        result = _post("order/cancel.json", {"awb_numbers": str(awb_number)})
    except Exception as e:
        return False, f"Could not reach iThink Logistics: {e}", None
    entry = (result.get("data") or {}).get("1") or {}
    if entry.get("status") == "Success":
        return True, "Shipment cancelled.", entry
    return False, entry.get("remark") or "Cancellation failed.", None
