from datetime import UTC, datetime

from shop.db import Database
from shop.products import ProductService


class OrderService:
    def __init__(self, database: Database, products: ProductService) -> None:
        self.db = database
        self.products = products

    def create_order(self, user_id: int, product_id: int, quantity: int) -> dict:
        product = self.products.get_product(product_id)
        return self.db.orders.insert(
            {
                "user_id": user_id,
                "product_id": product_id,
                "quantity": quantity,
                "total": product["price"],
                "status": "CREATED",
                "created_at": datetime.now(UTC).isoformat(),
            }
        )

    def complete_order(self, order_id: int) -> dict:
        return self.db.orders.update(order_id, status="COMPLETED")

    def list_orders(self, user_id: int) -> list[dict]:
        orders = [o for o in self.db.orders.all() if o["user_id"] == user_id]
        return sorted(orders, key=lambda o: o["created_at"], reverse=True)
