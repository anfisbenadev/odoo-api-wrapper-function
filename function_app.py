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
ODOO_URL = os.environ.get("ODOO_URL")  # e.g. https://miempresa.odoo.com
ODOO_DB = os.environ.get("ODOO_DB")  # e.g. miempresa-prod
ODOO_USER = os.environ.get("ODOO_USER")  # e.g. integracion@miempresa.cl
ODOO_PASSWORD = os.environ.get("ODOO_PASSWORD")  # API key or password


def get_odoo_uid(common_proxy):
    """Authenticate against Odoo and return the uid."""
    uid = common_proxy.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
    if not uid:
        raise Exception(
            "Odoo authentication failed - check ODOO_DB/ODOO_USER/ODOO_PASSWORD"
        )
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


def chunk_list(lst, size):
    for i in range(0, len(lst), size):
        yield lst[i : i + size]


def build_date_range(req: func.HttpRequest):
    """Resolve date_from / date_to from either explicit params or `days`."""
    date_from_param = req.params.get("date_from")  # YYYY-MM-DD
    date_to_param = req.params.get("date_to")  # YYYY-MM-DD

    if date_from_param:
        date_from = f"{date_from_param} 00:00:00"
    else:
        days = int(req.params.get("days", 120))  # ~4 months default
        date_from = (datetime.utcnow() - timedelta(days=days)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )

    if date_to_param:
        date_to = f"{date_to_param} 23:59:59"
    else:
        date_to = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    return date_from, date_to


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
            ODOO_DB,
            uid,
            ODOO_PASSWORD,
            "sale.order",
            "search_read",
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


# ---------------------------------------------------------------------------
# NEW: SKU demand analysis, sourced from sale.order.line (order detail),
# enriched with product master data and current/forecasted stock.
#
# Use cases:
#   GET /api/sales-demand                       -> full line-level detail
#   GET /api/sales-demand?aggregate=true         -> ranking by SKU (demand)
#
# Query params (all optional):
#   days                 int, default 120 (~4 months)
#   date_from, date_to   YYYY-MM-DD, overrides `days` if provided
#   states               csv, default "sale,done"
#   partner_id            int, filter to a single client
#   product_default_code  string, filter to a single SKU
#   limit                 int, 0 = no limit (applies to sale.order.line rows)
#   include_stock          "true"/"false", default "true"
#   aggregate               "true"/"false", default "false"
# ---------------------------------------------------------------------------
@app.function_name(name="GetOdooSalesDemand")
@app.route(route="sales-demand", methods=["GET"], auth_level=func.AuthLevel.FUNCTION)
def get_odoo_sales_demand(req: func.HttpRequest) -> func.HttpResponse:
    logging.info("GetOdooSalesDemand triggered")

    if not all([ODOO_URL, ODOO_DB, ODOO_USER, ODOO_PASSWORD]):
        return func.HttpResponse(
            json.dumps({"error": "Missing Odoo configuration in Application Settings"}),
            status_code=500,
            mimetype="application/json",
        )

    states_param = req.params.get("states", "sale,done")
    states = [s.strip() for s in states_param.split(",") if s.strip()]
    limit = int(req.params.get("limit", 0))
    partner_id_param = req.params.get("partner_id")
    product_code_param = req.params.get("product_default_code")
    include_stock = req.params.get("include_stock", "true").lower() != "false"
    aggregate = req.params.get("aggregate", "false").lower() == "true"

    date_from, date_to = build_date_range(req)

    try:
        common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
        uid = get_odoo_uid(common)
        models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")

        # -------------------------------------------------------------
        # 1) Header: sale.order in the date/state range (+ optional client)
        # -------------------------------------------------------------
        order_domain = [
            ("date_order", ">=", date_from),
            ("date_order", "<=", date_to),
            ("state", "in", states),
        ]
        if partner_id_param:
            order_domain.append(("partner_id", "=", int(partner_id_param)))

        order_fields = [
            "name",
            "date_order",
            "partner_id",
            "user_id",
            "team_id",
            "currency_id",
            "state",
        ]
        orders = models.execute_kw(
            ODOO_DB,
            uid,
            ODOO_PASSWORD,
            "sale.order",
            "search_read",
            [order_domain],
            {"fields": order_fields},
        )

        if not orders:
            return func.HttpResponse(
                json.dumps([]),
                status_code=200,
                mimetype="application/json",
            )

        orders_by_id = {o["id"]: flatten(o) for o in orders}
        order_ids = list(orders_by_id.keys())

        # -------------------------------------------------------------
        # 2) Optional SKU filter: resolve default_code -> product ids first
        # -------------------------------------------------------------
        product_id_filter = None
        if product_code_param:
            matching_products = models.execute_kw(
                ODOO_DB,
                uid,
                ODOO_PASSWORD,
                "product.product",
                "search_read",
                [[("default_code", "=", product_code_param)]],
                {"fields": ["id"]},
            )
            product_id_filter = [p["id"] for p in matching_products]
            if not product_id_filter:
                return func.HttpResponse(
                    json.dumps([]),
                    status_code=200,
                    mimetype="application/json",
                )

        # -------------------------------------------------------------
        # 3) Detail: sale.order.line for those orders (real product lines only)
        # -------------------------------------------------------------
        line_fields = [
            "order_id",
            "product_id",
            "name",
            "product_uom_qty",
            "qty_delivered",
            "product_uom_id",
            "price_unit",
            "discount",
            "price_subtotal",
            "price_tax",
            "price_total",
        ]

        all_lines = []
        for id_chunk in chunk_list(order_ids, 500):
            line_domain = [
                ("order_id", "in", id_chunk),
                ("product_id", "!=", False),  # excludes section/note lines
            ]
            if product_id_filter:
                line_domain.append(("product_id", "in", product_id_filter))

            kwargs = {"fields": line_fields}
            if limit:
                kwargs["limit"] = limit

            lines = models.execute_kw(
                ODOO_DB,
                uid,
                ODOO_PASSWORD,
                "sale.order.line",
                "search_read",
                [line_domain],
                kwargs,
            )
            all_lines.extend(lines)
            if limit and len(all_lines) >= limit:
                all_lines = all_lines[:limit]
                break

        if not all_lines:
            return func.HttpResponse(
                json.dumps([]),
                status_code=200,
                mimetype="application/json",
            )

        # -------------------------------------------------------------
        # 4) Enrich: product master data (SKU, category, cost, stock)
        # -------------------------------------------------------------
        product_ids = sorted(
            {l["product_id"][0] for l in all_lines if l.get("product_id")}
        )

        product_fields = [
            "default_code",
            "name",
            "categ_id",
            "uom_id",
            "list_price",
            "standard_price",
            "type",
        ]
        if include_stock:
            product_fields += ["qty_available", "virtual_available"]

        products_by_id = {}
        for id_chunk in chunk_list(product_ids, 500):
            products = models.execute_kw(
                ODOO_DB,
                uid,
                ODOO_PASSWORD,
                "product.product",
                "search_read",
                [[("id", "in", id_chunk)]],
                {"fields": product_fields},
            )
            for p in products:
                products_by_id[p["id"]] = flatten(p)

        # -------------------------------------------------------------
        # 5) Merge order + line + product into flat rows
        # -------------------------------------------------------------
        rows = []
        for line in all_lines:
            flat_line = flatten(line)
            order_id = flat_line.pop("order_id_id", None)
            flat_line.pop("order_id_name", None)

            order_info = orders_by_id.get(order_id, {})
            product_id = flat_line.get("product_id_id")
            product_info = products_by_id.get(product_id, {})

            row = {
                "order_id": order_id,
                "order_name": order_info.get("name"),
                "order_date": order_info.get("date_order"),
                "order_state": order_info.get("state"),
                "partner_id": order_info.get("partner_id_id"),
                "partner_name": order_info.get("partner_id_name"),
                "salesperson_id": order_info.get("user_id_id"),
                "salesperson_name": order_info.get("user_id_name"),
                "sales_team": order_info.get("team_id_name"),
                "currency": order_info.get("currency_id_name"),
                "product_id": product_id,
                "sku": product_info.get("default_code"),
                "product_name": product_info.get("name") or flat_line.get("name"),
                "product_category": product_info.get("categ_id_name"),
                "product_type": product_info.get("type"),
                "uom": flat_line.get("product_uom_id_name"),
                "qty_ordered": flat_line.get("product_uom_qty"),
                "qty_delivered": flat_line.get("qty_delivered"),
                "unit_price": flat_line.get("price_unit"),
                "discount_pct": flat_line.get("discount"),
                "subtotal": flat_line.get("price_subtotal"),
                "tax": flat_line.get("price_tax"),
                "total": flat_line.get("price_total"),
                "unit_cost": product_info.get("standard_price"),
            }

            if row["unit_cost"] is not None and row["qty_ordered"] is not None:
                row["margin_estimate"] = round(
                    row["subtotal"] - (row["unit_cost"] * row["qty_ordered"]), 2
                )
            else:
                row["margin_estimate"] = None

            if include_stock:
                row["stock_on_hand"] = product_info.get("qty_available")
                row["stock_forecasted"] = product_info.get("virtual_available")

            rows.append(row)

        # -------------------------------------------------------------
        # 6) Optional aggregation: ranking of SKUs by demand
        # -------------------------------------------------------------
        if aggregate:
            summary = {}
            for r in rows:
                key = r["sku"] or f"product_id_{r['product_id']}"
                if key not in summary:
                    summary[key] = {
                        "sku": r["sku"],
                        "product_id": r["product_id"],
                        "product_name": r["product_name"],
                        "product_category": r["product_category"],
                        "uom": r["uom"],
                        "qty_ordered_total": 0,
                        "qty_delivered_total": 0,
                        "revenue_total": 0.0,  # neto (price_subtotal), sin IVA
                        "revenue_total_taxed": 0.0,  # con IVA (price_total), referencia
                        "cost_total": 0.0,
                        "margin_total": 0.0,
                        "order_ids": set(),
                        "stock_on_hand": r.get("stock_on_hand"),
                        "stock_forecasted": r.get("stock_forecasted"),
                    }
                s = summary[key]
                qty_r = r["qty_ordered"] or 0
                s["qty_ordered_total"] += qty_r
                s["qty_delivered_total"] += r["qty_delivered"] or 0
                s["revenue_total"] += r["subtotal"] or 0
                s["revenue_total_taxed"] += r["total"] or 0
                if r.get("unit_cost") is not None:
                    s["cost_total"] += r["unit_cost"] * qty_r
                s["margin_total"] += r["margin_estimate"] or 0
                s["order_ids"].add(r["order_id"])

            result = []
            for s in summary.values():
                order_count = len(s.pop("order_ids"))
                qty = s["qty_ordered_total"]
                s["order_count"] = order_count
                # avg_price: precio de venta unitario promedio, NETO (sin IVA),
                # ponderado por cantidad -> revenue_total (neto) / qty_ordered_total
                s["avg_price"] = round(s["revenue_total"] / qty, 2) if qty else None
                # avg_cost: costo unitario promedio ponderado, desde standard_price
                s["avg_cost"] = round(s["cost_total"] / qty, 2) if qty else None
                s["revenue_total"] = round(s["revenue_total"], 2)
                s["revenue_total_taxed"] = round(s["revenue_total_taxed"], 2)
                s["cost_total"] = round(s["cost_total"], 2)
                s["margin_total"] = round(s["margin_total"], 2)
                result.append(s)

            result.sort(key=lambda x: x["qty_ordered_total"], reverse=True)

            return func.HttpResponse(
                json.dumps(result, default=str),
                status_code=200,
                mimetype="application/json",
            )

        return func.HttpResponse(
            json.dumps(rows, default=str),
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
