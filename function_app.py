import azure.functions as func
import logging
import json
import os
import xmlrpc.client
from datetime import datetime, timedelta

app = func.FunctionApp()

# ---------------------------------------------------------------------------
# Config: set these as Application Settings in Azure (or local.settings.json
# for local testing). Never hardcode credentials in this file.
# ---------------------------------------------------------------------------
ODOO_URL = os.environ.get("ODOO_URL")        # e.g. https://miempresa.odoo.com
ODOO_DB = os.environ.get("ODOO_DB")          # e.g. miempresa-prod
ODOO_USER = os.environ.get("ODOO_USER")      # e.g. integracion@miempresa.cl
ODOO_PASSWORD = os.environ.get("ODOO_PASSWORD")  # API key or password


def get_odoo_uid(common_proxy):
    """Authenticate against Odoo and return the uid."""
    uid = common_proxy.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
    if not uid:
        raise Exception("Odoo authentication failed - check ODOO_DB/ODOO_USER/ODOO_PASSWORD")
    return uid


def flatten(record: dict) -> dict:
    """Odoo many2one fields come back as [id, 'Display Name'].
    Power BI wants flat scalar columns, so split each into _id / _name."""
    flat = {}
    for key, value in record.items():
        if isinstance(value, list) and len(value) == 2 and isinstance(value[0], int):
            flat[f"{key}_id"] = value[0]
            flat[f"{key}_name"] = value[1]
        elif isinstance(value, bool) and value is False:
            # Odoo uses False instead of null for empty fields
            flat[key] = None
        else:
            flat[key] = value
    return flat


@app.function_name(name="GetOdooSales")
@app.route(route="sales", methods=["GET"], auth_level=func.AuthLevel.FUNCTION)
def get_odoo_sales(req: func.HttpRequest) -> func.HttpResponse:
    logging.info("GetOdooSales triggered")

    if not all([ODOO_URL, ODOO_DB, ODOO_USER, ODOO_PASSWORD]):
        return func.HttpResponse(
            json.dumps({"error": "Missing Odoo configuration in Application Settings"}),
            status_code=500,
            mimetype="application/json",
        )

    # Optional query params: ?days=30&limit=0&states=sale,done
    days = int(req.params.get("days", 30))
    limit = int(req.params.get("limit", 0))  # 0 = no limit
    states_param = req.params.get("states", "sale,done")
    states = [s.strip() for s in states_param.split(",") if s.strip()]

    date_from = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")

    domain = [
        ("date_order", ">=", date_from),
        ("state", "in", states),
    ]

    fields = [
        "name",
        "date_order",
        "partner_id",
        "user_id",
        "team_id",
        "amount_untaxed",
        "amount_tax",
        "amount_total",
        "state",
        "currency_id",
    ]

    try:
        common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
        uid = get_odoo_uid(common)

        models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")

        kwargs = {"fields": fields, "order": "date_order desc"}
        if limit:
            kwargs["limit"] = limit

        orders = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "sale.order", "search_read",
            [domain],
            kwargs,
        )

        result = [flatten(o) for o in orders]

        return func.HttpResponse(
            json.dumps(result, default=str),
            status_code=200,
            mimetype="application/json",
        )

    except xmlrpc.client.Fault as fault:
        logging.error(f"Odoo XML-RPC fault: {fault}")
        return func.HttpResponse(
            json.dumps({"error": "Odoo XML-RPC fault", "detail": str(fault)}),
            status_code=502,
            mimetype="application/json",
        )
    except Exception as e:
        logging.exception("Unexpected error calling Odoo")
        return func.HttpResponse(
            json.dumps({"error": "Unexpected error", "detail": str(e)}),
            status_code=500,
            mimetype="application/json",
        )