import json
import urllib.request

from shop.db import Database

PG_URL = "https://api.pg.example.com/v1/payments/approve"


class PgError(Exception):
    pass


class PgClient:
    def __init__(self, url: str = PG_URL, timeout: float = 3.0) -> None:
        self.url = url
        self.timeout = timeout

    def approve(self, order_id: int, amount: int) -> dict:
        body = json.dumps({"order_id": order_id, "amount": amount}).encode()
        request = urllib.request.Request(
            self.url, data=body, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read())
        except OSError as e:
            raise PgError(str(e)) from e


class PaymentService:
    def __init__(self, database: Database, pg: PgClient) -> None:
        self.db = database
        self.pg = pg

    def pay(self, order_id: int) -> dict:
        order = self.db.orders.get(order_id)
        result = self.pg.approve(order_id, order["total"])
        return self.db.payments.insert(
            {"order_id": order_id, "amount": order["total"], "pg_tid": result["tid"]}
        )
