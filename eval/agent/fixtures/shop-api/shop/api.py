"""HTTP 라우트 → 서비스 함수. 운영에서는 이 표를 웹 프레임워크에 등록한다."""

from shop.db import db
from shop.orders import OrderService
from shop.payments import PaymentService, PgClient
from shop.products import ProductService

products = ProductService(db)
orders = OrderService(db, products)
payments = PaymentService(db, PgClient())

ROUTES = {
    ("GET", "/products/{id}"): lambda req: products.get_product(int(req["id"])),
    ("GET", "/products"): lambda req: products.search_products(req.get("q", "")),
    ("POST", "/orders"): lambda req: orders.create_order(
        int(req["user_id"]), int(req["product_id"]), int(req["quantity"])
    ),
    ("GET", "/orders"): lambda req: orders.list_orders(int(req["user_id"])),
    ("POST", "/orders/{id}/pay"): lambda req: payments.pay(int(req["id"])),
}
