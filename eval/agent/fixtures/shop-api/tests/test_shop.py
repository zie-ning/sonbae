import unittest

from shop.db import Database
from shop.orders import OrderService
from shop.payments import PaymentService
from shop.products import ProductNotFound, ProductService


class FakePg:
    def __init__(self) -> None:
        self.calls = []

    def approve(self, order_id: int, amount: int) -> dict:
        self.calls.append((order_id, amount))
        return {"tid": f"T{order_id}"}


class ShopTest(unittest.TestCase):
    def setUp(self) -> None:
        self.db = Database()
        self.products = ProductService(self.db)
        self.orders = OrderService(self.db, self.products)
        self.product = self.products.create_product("키보드", price=50000, stock=10)

    def test_get_missing_product(self) -> None:
        with self.assertRaises(ProductNotFound):
            self.products.get_product(999)

    def test_search_products(self) -> None:
        self.products.create_product("마우스", price=20000, stock=5)
        self.assertEqual([p["name"] for p in self.products.search_products("키보")], ["키보드"])

    def test_create_order(self) -> None:
        order = self.orders.create_order(user_id=1, product_id=self.product["id"], quantity=1)
        self.assertEqual(order["status"], "CREATED")
        self.assertEqual(order["total"], 50000)

    def test_list_orders(self) -> None:
        self.orders.create_order(user_id=1, product_id=self.product["id"], quantity=1)
        self.orders.create_order(user_id=2, product_id=self.product["id"], quantity=1)
        self.assertEqual(len(self.orders.list_orders(1)), 1)

    def test_pay(self) -> None:
        pg = FakePg()
        order = self.orders.create_order(user_id=1, product_id=self.product["id"], quantity=1)
        payment = PaymentService(self.db, pg).pay(order["id"])
        self.assertEqual(payment["pg_tid"], f"T{order['id']}")


if __name__ == "__main__":
    unittest.main()
