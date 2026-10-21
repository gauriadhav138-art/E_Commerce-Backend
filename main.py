# main.py  -  all API routes.
# Run with:  uvicorn main:app --reload
# Then open http://localhost:8000/docs to test every route in the browser.

import re
import uuid
from datetime import date
from pathlib import Path

import mysql.connector
from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from auth import create_token, get_current_user, hash_password, require_admin, require_seller, verify_password
from database import get_connection  # your existing connection function
from db_helpers import call_procedure, execute, fetch_all, fetch_one

app = FastAPI(
    title="E-Commerce API",
    description="Backend for the Multi-Vendor E-Commerce System",
    version="1.0.0"
)

# Websites that are allowed to call this backend.
# 5173 = your Vite React app, 5500 = VS Code Live Server (your old HTML pages)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://127.0.0.1:5500",
        "http://localhost:5500",
        "null"
    ],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"]
)


# Product photos are saved in the "uploads" folder (next to main.py) and shown at
#   http://localhost:8000/uploads/<file name>
UPLOAD_DIR = Path(__file__).parent / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")


# ---------------------------------------------------------------------
# ERROR HANDLING
# Our triggers/procedures use SIGNAL (sqlstate 45000) for business errors
# like "Not enough stock". We turn those into a normal 400 error message.
# ---------------------------------------------------------------------
@app.exception_handler(mysql.connector.Error)
def mysql_error_handler(request, error):
    if error.sqlstate == "45000":
        return JSONResponse(status_code=400, content={"detail": error.msg})
    print("DATABASE ERROR:", error)  # shows in the uvicorn terminal
    return JSONResponse(status_code=500, content={"detail": f"Database error: {error.msg}"})


# ---------------------------------------------------------------------
# REQUEST BODIES (what React must send). Pydantic checks them for us.
# ---------------------------------------------------------------------
class RegisterIn(BaseModel):
    name: str
    email: str
    password: str = Field(min_length=6)
    phone: str | None = None
    role: str = "customer"          # NEW: "customer" or "seller"


class LoginIn(BaseModel):
    email: str
    password: str
    role: str | None = None         # NEW: the login type chosen on the login page


class CartIn(BaseModel):
    product_id: int
    quantity: int = Field(default=1, gt=0)


class StatusIn(BaseModel):
    status: str


class PriceIn(BaseModel):
    price: float = Field(gt=0)


class StockIn(BaseModel):
    quantity: int = Field(gt=0)


class ProductIn(BaseModel):
    category_id: int
    product_name: str
    description: str | None = None
    price: float = Field(gt=0)
    quantity: int = Field(default=0, ge=0)
    reorder_level: int = 10


# ---------------------------------------------------------------------
# PAYMENT: what React sends at checkout, and the checks we run on it.
# (This is a DEMO payment: no real money moves. Full card numbers and CVV are
#  checked and then thrown away. Only "Card ending 1234" is saved.)
# ---------------------------------------------------------------------
class CheckoutIn(BaseModel):
    method: str                      # COD, UPI, Card or NetBanking
    upi_id: str | None = None
    card_number: str | None = None
    card_name: str | None = None
    expiry: str | None = None        # MM/YY
    cvv: str | None = None
    bank: str | None = None


BANKS = ["State Bank of India", "HDFC Bank", "ICICI Bank", "Axis Bank", "Kotak Mahindra Bank"]


def check_payment_details(data):
    """Validate the payment form. Returns the short text we are allowed to save."""
    if data.method == "COD":
        return "Cash on delivery"

    if data.method == "UPI":
        if not data.upi_id or not re.fullmatch(r"[\w.\-]{2,}@[A-Za-z]{2,}", data.upi_id.strip()):
            raise HTTPException(400, "Enter a valid UPI id, for example name@upi")
        return "UPI " + data.upi_id.strip()

    if data.method == "Card":
        number = re.sub(r"\D", "", data.card_number or "")
        if len(number) != 16:
            raise HTTPException(400, "Card number must be 16 digits")
        if not data.card_name or len(data.card_name.strip()) < 2:
            raise HTTPException(400, "Enter the name on the card")
        match = re.fullmatch(r"(0[1-9]|1[0-2])/(\d{2})", (data.expiry or "").strip())
        if not match:
            raise HTTPException(400, "Expiry must look like MM/YY")
        year, month = 2000 + int(match.group(2)), int(match.group(1))
        if (year, month) < (date.today().year, date.today().month):
            raise HTTPException(400, "This card has expired")
        if not re.fullmatch(r"\d{3,4}", data.cvv or ""):
            raise HTTPException(400, "CVV must be 3 or 4 digits")
        return "Card ending " + number[-4:]          # never save the full number

    if data.method == "NetBanking":
        if data.bank not in BANKS:
            raise HTTPException(400, "Please choose your bank")
        return "NetBanking " + data.bank

    raise HTTPException(400, "Please choose a payment method")


# ---------------------------------------------------------------------
# PRODUCT IMAGES: the file goes into the uploads folder, the database
# stores only its path, for example  /uploads/3f9a2c0d1b.png
# ---------------------------------------------------------------------
ALLOWED_IMAGE_TYPES = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
MAX_IMAGE_SIZE = 2 * 1024 * 1024          # 2 MB


def looks_like_image(content):
    """Check the first bytes of the file (not just its name) to be sure it is a picture."""
    return (
        content.startswith(b"\xff\xd8\xff")                          # JPEG
        or content.startswith(b"\x89PNG\r\n\x1a\n")                  # PNG
        or content.startswith(b"GIF8")                                 # GIF
        or (content[:4] == b"RIFF" and content[8:12] == b"WEBP")       # WEBP
    )


def save_image(file: UploadFile):
    """Check the uploaded file, save it, and return its path."""
    extension = Path(file.filename or "").suffix.lower()
    if extension not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(400, "Image must be a .jpg, .png, .webp or .gif file")

    content = file.file.read(MAX_IMAGE_SIZE + 1)
    if len(content) > MAX_IMAGE_SIZE:
        raise HTTPException(400, "Image must be smaller than 2 MB")
    if not looks_like_image(content):
        raise HTTPException(400, "This file is not a real image")

    file_name = uuid.uuid4().hex[:20] + extension    # random name, so files never overwrite each other
    (UPLOAD_DIR / file_name).write_bytes(content)
    return "/uploads/" + file_name


def create_product(owner_id, category_id, product_name, description, price, quantity, reorder_level, image):
    image_url = save_image(image) if image is not None and image.filename else ""
    product_id = execute(
        "INSERT INTO products (seller_id, category_id, product_name, description, price, image_url, status, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, 'active', NOW())",
        (owner_id, category_id, product_name, description, price, image_url),
    )
    execute(
        "INSERT INTO inventory (product_id, quantity, reorder_level) VALUES (%s, %s, %s)",
        (product_id, quantity, reorder_level),
    )
    return {"message": "Product added", "product_id": product_id, "image_url": image_url}


def set_product_image(product_id, image):
    image_url = save_image(image)
    execute("UPDATE products SET image_url = %s WHERE product_id = %s", (image_url, product_id))
    return {"message": "Image updated", "image_url": image_url}


# =====================================================================
# YOUR ORIGINAL ROUTES (unchanged)
# =====================================================================

# 1. Home endpoint
@app.get("/")
def home():
    return {
        "message": "E-Commerce API is running"
    }


# 2. Test MySQL connection
@app.get("/test-db")
def test_database():
    connection = None
    cursor = None

    try:
        connection = get_connection()
        cursor = connection.cursor(buffered=True)

        cursor.execute("SELECT DATABASE()")
        result = cursor.fetchone()

        return {
            "database": result[0],
            "message": "MySQL connection successful"
        }

    except Exception as error:
        raise HTTPException(
            status_code=500,
            detail=f"Database connection failed: {str(error)}"
        )

    finally:
        if cursor is not None:
            cursor.close()

        if connection is not None and connection.is_connected():
            connection.close()


# 3. Get all products
@app.get("/products")
def get_products_original():
    connection = None
    cursor = None

    try:
        connection = get_connection()

        # Return each row as a dictionary
        cursor = connection.cursor(dictionary=True)

        # Execute the SQL query
        cursor.execute("SELECT * FROM products")

        # Retrieve all product records
        products = cursor.fetchall()

        return products

    except Exception as error:
        raise HTTPException(
            status_code=500,
            detail=f"Could not retrieve products: {str(error)}"
        )

    finally:
        if cursor is not None:
            cursor.close()

        if connection is not None and connection.is_connected():
            connection.close()


@app.get("/products-by-category/{category_name}")
def get_products_by_category(category_name: str):
    connection = None
    cursor = None

    try:
        connection = get_connection()
        cursor = connection.cursor(dictionary=True)

        query = """
            SELECT
                p.product_id,
                p.product_name,
                p.description,
                p.price,
                p.image_url,
                c.category_name
            FROM products p
            LEFT JOIN categories c
                ON p.category_id = c.category_id
            WHERE c.category_name = %s
            ORDER BY p.product_name
        """

        cursor.execute(query, (category_name,))
        products = cursor.fetchall()

        for product in products:
            if product["price"] is not None:
                product["price"] = float(product["price"])

        return products

    except Exception as error:
        raise HTTPException(
            status_code=500,
            detail=f"Could not retrieve category products: {str(error)}"
        )

    finally:
        if cursor is not None:
            cursor.close()
        if connection is not None and connection.is_connected():
            connection.close()


# =====================================================================
# NEW ROUTES (all start with /api)
# =====================================================================
# =====================================================================
# 1. AUTH
# =====================================================================
@app.post("/api/register", status_code=201)
def register(data: RegisterIn):
    # Anyone can sign up as a customer or a seller. Admins are never created here.
    if data.role not in ("customer", "seller"):
        raise HTTPException(400, "You can register only as a customer or a seller")

    if fetch_one("SELECT user_id FROM users WHERE email = %s", (data.email,)):
        raise HTTPException(400, "Email is already registered")

    user_id = execute(
        "INSERT INTO users (name, email, password, phone, role, created_at) "
        "VALUES (%s, %s, %s, %s, %s, NOW())",
        (data.name, data.email, hash_password(data.password), data.phone, data.role),
    )
    # only customers need a cart
    if data.role == "customer":
        execute("INSERT INTO cart (user_id) VALUES (%s)", (user_id,))
    return {"message": "Registered successfully", "user_id": user_id}


@app.post("/api/login")
def login(data: LoginIn):
    user = fetch_one("SELECT * FROM users WHERE email = %s", (data.email,))
    if not user or not verify_password(data.password, user["password"]):
        raise HTTPException(401, "Wrong email or password")

    # NEW: the login type chosen on the page must match the account's real role
    if data.role and user["role"] != data.role:
        raise HTTPException(
            403, f"This account is not registered as {data.role}. Please choose the correct login type."
        )

    return {
        "token": create_token(user),
        "user": {"user_id": user["user_id"], "name": user["name"], "role": user["role"]},
    }


# =====================================================================
# 2. PRODUCTS AND CATEGORIES  (public)  -> uses the VIEW vw_product_details
# =====================================================================
@app.get("/api/categories")
def get_categories():
    return fetch_all("SELECT category_id, category_name, description FROM categories")


@app.get("/api/products")
def get_products(category: str | None = None, search: str | None = None):
    sql = "SELECT * FROM vw_product_details WHERE status = 'active'"
    params = []
    if category:
        sql += " AND category_name = %s"
        params.append(category)
    if search:
        sql += " AND product_name LIKE %s"
        params.append(f"%{search}%")
    sql += " ORDER BY product_id"
    return fetch_all(sql, tuple(params))


@app.get("/api/products/{product_id}")
def get_product(product_id: int):
    product = fetch_one("SELECT * FROM vw_product_details WHERE product_id = %s", (product_id,))
    if not product:
        raise HTTPException(404, "Product not found")
    return product


# =====================================================================
# 3. CART  (logged-in customer)  -> JOIN query + procedure sp_add_to_cart
# =====================================================================
@app.get("/api/cart")
def get_cart(user=Depends(get_current_user)):
    items = fetch_all(
        """
        SELECT p.product_id, p.product_name, p.price, p.image_url, ci.quantity,
               (ci.quantity * p.price) AS item_total
        FROM cart c
        JOIN cart_items ci ON c.cart_id = ci.cart_id
        JOIN products p    ON ci.product_id = p.product_id
        WHERE c.user_id = %s
        """,
        (user["user_id"],),
    )
    total = sum(item["item_total"] for item in items)
    return {"items": items, "total": total}


@app.post("/api/cart", status_code=201)
def add_to_cart(data: CartIn, user=Depends(get_current_user)):
    call_procedure("sp_add_to_cart", [user["user_id"], data.product_id, data.quantity])
    return {"message": "Added to cart"}


@app.delete("/api/cart/{product_id}")
def remove_from_cart(product_id: int, user=Depends(get_current_user)):
    execute(
        """
        DELETE ci FROM cart_items ci
        JOIN cart c ON ci.cart_id = c.cart_id
        WHERE c.user_id = %s AND ci.product_id = %s
        """,
        (user["user_id"], product_id),
    )
    return {"message": "Removed from cart"}


# =====================================================================
# 4. ORDERS  (logged-in customer)
#    sp_place_order  -> TRANSACTION + triggers (check/reduce stock)
#    sp_cancel_order -> puts stock back
# =====================================================================
@app.post("/api/orders", status_code=201)
def place_order(user=Depends(get_current_user)):
    out_args, _ = call_procedure("sp_place_order", [user["user_id"], 0])
    order_id = out_args[1]  # the OUT parameter = new order id
    return {"message": "Order placed", "order_id": order_id}


@app.post("/api/checkout", status_code=201)
def checkout(data: CheckoutIn, user=Depends(get_current_user)):
    # Order + payment in ONE transaction (stored procedure sp_checkout)
    if user["role"] != "customer":
        raise HTTPException(403, "Only customers can place orders")

    details = check_payment_details(data)                       # 1. check the form
    txn = None if data.method == "COD" else "TXN" + uuid.uuid4().hex[:12].upper()   # 2. demo transaction id
    out_args, _ = call_procedure("sp_checkout", [user["user_id"], data.method, details, txn, 0])
    order_id = out_args[4]                                      # the OUT parameter = new order id

    payment = fetch_one(
        "SELECT method, amount, status, transaction_id FROM payments WHERE order_id = %s", (order_id,)
    )
    return {"message": "Order placed", "order_id": order_id, "payment": payment}


@app.get("/api/orders")
def my_orders(user=Depends(get_current_user)):
    return fetch_all(
        """
        SELECT o.order_id, o.order_date, o.total_amount, o.status,
               pay.method AS payment_method, pay.status AS payment_status
        FROM orders o
        LEFT JOIN payments pay ON o.order_id = pay.order_id
        WHERE o.user_id = %s
        ORDER BY o.order_date DESC
        """,
        (user["user_id"],),
    )


def get_order_or_404(order_id, user):
    """Find an order and make sure this user is allowed to see it."""
    order = fetch_one("SELECT * FROM orders WHERE order_id = %s", (order_id,))
    if not order:
        raise HTTPException(404, "Order not found")
    if order["user_id"] != user["user_id"] and user["role"] != "admin":
        raise HTTPException(403, "This is not your order")
    return order


@app.get("/api/orders/{order_id}")
def order_details(order_id: int, user=Depends(get_current_user)):
    order = get_order_or_404(order_id, user)
    order["payment"] = fetch_one(
        "SELECT method, amount, status, transaction_id, payment_details, created_at "
        "FROM payments WHERE order_id = %s", (order_id,)
    )
    order["items"] = fetch_all(
        """
        SELECT p.product_name, oi.quantity, oi.price,
               (oi.quantity * oi.price) AS line_total
        FROM order_items oi
        JOIN products p ON oi.product_id = p.product_id
        WHERE oi.order_id = %s
        """,
        (order_id,),
    )
    return order


@app.post("/api/orders/{order_id}/cancel")
def cancel_order(order_id: int, user=Depends(get_current_user)):
    get_order_or_404(order_id, user)
    call_procedure("sp_cancel_order", [order_id])
    return {"message": "Order cancelled and stock restored"}


# =====================================================================
# 5. ADMIN  -> views, procedures, cursor, triggers
# =====================================================================
@app.get("/api/admin/orders")
def admin_orders(admin=Depends(require_admin)):
    return fetch_all("SELECT * FROM vw_order_summary ORDER BY order_date DESC")


@app.put("/api/admin/orders/{order_id}/status")
def update_order_status(order_id: int, data: StatusIn, admin=Depends(require_admin)):
    allowed = ["Pending", "Confirmed", "Shipped", "Delivered", "Cancelled"]
    if data.status not in allowed:
        raise HTTPException(400, f"Status must be one of {allowed}")

    if data.status == "Cancelled":
        call_procedure("sp_cancel_order", [order_id])  # also restores stock
    else:
        execute("UPDATE orders SET status = %s WHERE order_id = %s", (data.status, order_id))
    return {"message": f"Order status is now {data.status}"}


@app.get("/api/admin/low-stock")
def low_stock(admin=Depends(require_admin)):
    return fetch_all("SELECT * FROM vw_low_stock")            # VIEW


@app.get("/api/admin/stock-alerts")
def stock_alerts(admin=Depends(require_admin)):
    return fetch_all("SELECT * FROM stock_alerts ORDER BY alert_time DESC")  # filled by TRIGGER


@app.get("/api/admin/price-history")
def price_history(admin=Depends(require_admin)):
    return fetch_all(                                          # filled by TRIGGER
        """
        SELECT ph.*, p.product_name
        FROM price_history ph
        JOIN products p ON ph.product_id = p.product_id
        ORDER BY ph.changed_at DESC
        """
    )


@app.get("/api/admin/sales-by-category")
def sales_by_category(admin=Depends(require_admin)):
    return fetch_all("SELECT * FROM vw_sales_by_category")    # VIEW


@app.get("/api/admin/sales-report")
def sales_report(
    date_from: date = Query(alias="from"),
    date_to: date = Query(alias="to"),
    admin=Depends(require_admin),
):
    # example:  /api/admin/sales-report?from=2025-03-01&to=2025-03-31
    _, result_sets = call_procedure("sp_sales_report", [date_from, date_to])
    return result_sets[0] if result_sets else []


@app.post("/api/admin/cancel-old-pending")
def cancel_old_pending(days: int = 30, admin=Depends(require_admin)):
    # runs the CURSOR procedure
    _, result_sets = call_procedure("sp_cancel_old_pending_orders", [days])
    return result_sets[0][0] if result_sets else {"orders_cancelled": 0}


@app.post("/api/admin/products", status_code=201)
def add_product(
    category_id: int = Form(...),
    product_name: str = Form(...),
    description: str = Form(""),
    price: float = Form(..., gt=0),
    quantity: int = Form(0, ge=0),
    reorder_level: int = Form(10, ge=0),
    image: UploadFile | None = File(None),          # the product photo (optional)
    admin=Depends(require_admin),
):
    return create_product(admin["user_id"], category_id, product_name, description,
                          price, quantity, reorder_level, image)


@app.post("/api/admin/products/{product_id}/image")
def admin_change_image(product_id: int, image: UploadFile = File(...), admin=Depends(require_admin)):
    return set_product_image(product_id, image)


@app.put("/api/admin/products/{product_id}/price")
def change_price(product_id: int, data: PriceIn, admin=Depends(require_admin)):
    # the trigger trg_price_history saves the old price automatically
    execute("UPDATE products SET price = %s WHERE product_id = %s", (data.price, product_id))
    return {"message": "Price updated"}


@app.put("/api/admin/inventory/{product_id}/restock")
def restock(product_id: int, data: StockIn, admin=Depends(require_admin)):
    execute(
        "UPDATE inventory SET quantity = quantity + %s WHERE product_id = %s",
        (data.quantity, product_id),
    )
    return {"message": f"Added {data.quantity} items to stock"}


# =====================================================================
# 6. SELLER  (role = seller)  -> a seller can only touch HIS OWN products
# =====================================================================
def check_my_product(product_id, seller):
    """Stop a seller from editing somebody else's product."""
    product = fetch_one(
        "SELECT product_id FROM products WHERE product_id = %s AND seller_id = %s",
        (product_id, seller["user_id"]),
    )
    if not product:
        raise HTTPException(404, "Product not found in your shop")


@app.get("/api/seller/products")
def seller_products(seller=Depends(require_seller)):
    return fetch_all(
        """
        SELECT p.product_id, p.product_name, p.price, p.status, p.image_url,
               c.category_name, i.quantity AS stock
        FROM products p
        JOIN categories c     ON p.category_id = c.category_id
        LEFT JOIN inventory i ON p.product_id  = i.product_id
        WHERE p.seller_id = %s
        ORDER BY p.product_id
        """,
        (seller["user_id"],),
    )


@app.post("/api/seller/products", status_code=201)
def seller_add_product(
    category_id: int = Form(...),
    product_name: str = Form(...),
    description: str = Form(""),
    price: float = Form(..., gt=0),
    quantity: int = Form(0, ge=0),
    reorder_level: int = Form(10, ge=0),
    image: UploadFile | None = File(None),          # the product photo (optional)
    seller=Depends(require_seller),
):
    return create_product(seller["user_id"], category_id, product_name, description,
                          price, quantity, reorder_level, image)


@app.post("/api/seller/products/{product_id}/image")
def seller_change_image(product_id: int, image: UploadFile = File(...), seller=Depends(require_seller)):
    check_my_product(product_id, seller)             # only for HIS OWN product
    return set_product_image(product_id, image)


@app.put("/api/seller/products/{product_id}/price")
def seller_change_price(product_id: int, data: PriceIn, seller=Depends(require_seller)):
    check_my_product(product_id, seller)
    execute("UPDATE products SET price = %s WHERE product_id = %s", (data.price, product_id))
    return {"message": "Price updated"}


@app.put("/api/seller/inventory/{product_id}/restock")
def seller_restock(product_id: int, data: StockIn, seller=Depends(require_seller)):
    check_my_product(product_id, seller)
    execute(
        "UPDATE inventory SET quantity = quantity + %s WHERE product_id = %s",
        (data.quantity, product_id),
    )
    return {"message": f"Added {data.quantity} items to stock"}


@app.get("/api/seller/sales")
def seller_sales(seller=Depends(require_seller)):
    # every order line that contains one of this seller's products (5-table JOIN)
    return fetch_all(
        """
        SELECT o.order_id, o.order_date, u.name AS customer, p.product_name,
               oi.quantity, oi.price, (oi.quantity * oi.price) AS line_total, o.status
        FROM order_items oi
        JOIN orders o   ON oi.order_id   = o.order_id
        JOIN products p ON oi.product_id = p.product_id
        JOIN users u    ON o.user_id     = u.user_id
        WHERE p.seller_id = %s
        ORDER BY o.order_date DESC
        """,
        (seller["user_id"],),
    )


@app.get("/api/admin/payments")
def admin_payments(admin=Depends(require_admin)):
    return fetch_all(
        """
        SELECT pay.payment_id, pay.order_id, u.name AS customer, pay.method,
               pay.amount, pay.status, pay.transaction_id, pay.created_at
        FROM payments pay
        JOIN orders o ON pay.order_id = o.order_id
        JOIN users u  ON o.user_id    = u.user_id
        ORDER BY pay.created_at DESC
        """
    )