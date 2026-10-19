# shop-api

쇼핑몰 주문 API 서버. 상품 조회·검색, 주문, 결제를 다룬다.

- Python 3.11+, 표준 라이브러리만 쓴다
- 운영에서는 MySQL을 쓰고, 이 저장소의 `shop/db.py`는 개발용 메모리 저장소다
- 서버 3대가 로드밸런서 뒤에서 돈다
- 결제는 외부 PG사 API(`shop/payments.py`의 `PgClient`)를 호출한다

## 구조

| 파일 | 내용 |
| --- | --- |
| `shop/db.py` | 테이블별 메모리 저장소 |
| `shop/products.py` | 상품 조회·검색 |
| `shop/orders.py` | 주문 생성·조회 |
| `shop/payments.py` | 결제 승인 (PG 호출) |
| `shop/api.py` | HTTP 라우트 → 서비스 함수 연결 |
| `tests/` | `unittest` 테스트 |
