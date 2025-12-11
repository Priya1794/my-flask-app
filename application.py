import os
import json
import boto3
import pymysql
from flask import Flask, request, render_template, redirect, url_for
from werkzeug.utils import secure_filename

app = application = Flask(__name__)

# --------------------------------------------------------
# CONFIG
# --------------------------------------------------------
REGION = os.environ.get("REGION", "ap-south-1")
BUCKET = os.environ.get("S3_BUCKET")
SQS_URL = os.environ.get("SQS_QUEUE_URL")

# Validate early to avoid hidden errors
if not BUCKET:
    raise Exception("❌ Environment variable S3_BUCKET is missing!")

# --------------------------------------------------------
# AWS CLIENT HELPERS
# --------------------------------------------------------
def ssm_client():
    return boto3.client("ssm", region_name=REGION)

def s3_client():
    return boto3.client("s3", region_name=REGION)

def sqs_client():
    return boto3.client("sqs", region_name=REGION) if SQS_URL else None

# --------------------------------------------------------
# SSM PARAMETER FETCH
# --------------------------------------------------------
def get_parameter(name):
    param = ssm_client().get_parameter(Name=name, WithDecryption=True)
    return param["Parameter"]["Value"]

# --------------------------------------------------------
# RDS DATABASE
# --------------------------------------------------------
def get_db_connection():
    return pymysql.connect(
        host=get_parameter("/ecommerce/db/host"),
        user=get_parameter("/ecommerce/db/username"),
        password=get_parameter("/ecommerce/db/password"),
        database=get_parameter("/ecommerce/db/databasename"),
        cursorclass=pymysql.cursors.DictCursor
    )

def get_all_products():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM products")
    rows = cur.fetchall()
    conn.close()
    return rows

def get_product(pid):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM products WHERE id=%s", (pid,))
    row = cur.fetchone()
    conn.close()
    return row

# --------------------------------------------------------
# S3 PRESIGNED URLS
# --------------------------------------------------------
def presigned_get_url(key, expires=3600):
    return s3_client().generate_presigned_url(
        "get_object",
        Params={"Bucket": BUCKET, "Key": key},
        ExpiresIn=expires
    )

# --------------------------------------------------------
# CART
# --------------------------------------------------------
cart = {}

# --------------------------------------------------------
# ROUTES
# --------------------------------------------------------

@app.route("/")
def home():
    products = get_all_products()

    for p in products:
        if p.get("image_url"):
            # database stores key only, e.g. "products/phone.jpg"
            p["image_url"] = presigned_get_url(p["image_url"])
        else:
            p["image_url"] = url_for("static", filename="placeholder.png")

    return render_template("index.html", products=products)


@app.route("/product/<int:pid>")
def product_page(pid):
    p = get_product(pid)
    if not p:
        return "Product not found", 404

    if p.get("image_url"):
        p["image_url"] = presigned_get_url(p["image_url"])
    else:
        p["image_url"] = url_for("static", filename="placeholder.png")

    return render_template("product.html", product=p)


@app.route("/cart")
def view_cart():
    items = []
    total = 0

    for pid, qty in cart.items():
        p = get_product(pid)
        if not p:
            continue

        if p.get("image_url"):
            p["image_url"] = presigned_get_url(p["image_url"])

        subtotal = p["price"] * qty
        items.append({"product": p, "qty": qty, "subtotal": subtotal})
        total += subtotal

    return render_template("cart.html", items=items, total=total)


@app.route("/cart/add/<int:pid>", methods=["POST"])
def add_to_cart(pid):
    cart[pid] = cart.get(pid, 0) + 1
    return redirect("/cart")

# --------------------------------------------------------
# CHECKOUT (WITH FILE UPLOAD + SQS)
# --------------------------------------------------------
@app.route("/checkout", methods=["GET", "POST"])
def checkout():
    if request.method == "GET":
        return render_template("checkout.html")

    name = request.form.get("name")
    email = request.form.get("email")

    order = {
        "name": name,
        "email": email,
        "items": cart
    }

    uploaded_file_url = None

    # -----------------------------
    # FILE UPLOAD TO S3
    # -----------------------------
    if "image" in request.files:
        f = request.files["image"]

        if f.filename:
            filename = secure_filename(f.filename)
            key = f"uploads/{filename}"

            # Upload to S3
            s3_client().upload_fileobj(f, BUCKET, key)

            # Presigned URL to display uploaded image
            uploaded_file_url = presigned_get_url(key)

    # -----------------------------
    # SEND ORDER TO SQS
    # -----------------------------
    sqs = sqs_client()
    sqs_sent = False

    if sqs:
        sqs.send_message(
            QueueUrl=SQS_URL,
            MessageBody=json.dumps(order)
        )
        sqs_sent = True

    # Clear cart
    cart.clear()

    return render_template(
        "checkout.html",
        success=True,
        order=order,
        file_url=uploaded_file_url,
        sqs_sent=sqs_sent
    )

# --------------------------------------------------------
# MAIN
# --------------------------------------------------------
if __name__ == "__main__":
    app.run(debug=True)

