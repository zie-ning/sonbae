from shop.db import Database


class ProductNotFound(Exception):
    pass


class ProductService:
    def __init__(self, database: Database) -> None:
        self.db = database

    def create_product(self, name: str, price: int, stock: int, description: str = "") -> dict:
        return self.db.products.insert(
            {"name": name, "price": price, "stock": stock, "description": description}
        )

    def get_product(self, product_id: int) -> dict:
        # 상세 화면마다 호출된다. 운영 DB에서는 상품·옵션·리뷰 요약을 조인해 느리다
        product = self.db.products.get(product_id)
        if product is None:
            raise ProductNotFound(product_id)
        return product

    def search_products(self, keyword: str) -> list[dict]:
        # 운영에서는 LIKE '%keyword%' 쿼리다
        return [
            p for p in self.db.products.all() if keyword in p["name"] or keyword in p["description"]
        ]
