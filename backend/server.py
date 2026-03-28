from dotenv import load_dotenv
from pathlib import Path
ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

from fastapi import FastAPI, APIRouter, HTTPException, Request, Response, WebSocket, WebSocketDisconnect, Body, Query
from fastapi.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
from bson import ObjectId
from pydantic import BaseModel
from typing import Optional, List, Dict
from datetime import datetime, timezone, timedelta
from math import radians, cos, sin, asin, sqrt
from emergentintegrations.payments.stripe.checkout import StripeCheckout, CheckoutSessionRequest
import os, logging, bcrypt, jwt, secrets

# MongoDB
mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ['DB_NAME']]

app = FastAPI()

cors_origins_raw = os.environ.get("CORS_ORIGINS", "*")
if cors_origins_raw == "*":
    cors_origins = [os.environ.get("FRONTEND_URL", "http://localhost:3000")]
else:
    cors_origins = [o.strip() for o in cors_origins_raw.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

api = APIRouter(prefix="/api")

# ===== AUTH HELPERS =====
JWT_SECRET = os.environ.get("JWT_SECRET", "fallback-secret-key-change-in-production")
JWT_ALG = "HS256"

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))

def create_access_token(user_id: str, email: str) -> str:
    return jwt.encode({"sub": user_id, "email": email, "exp": datetime.now(timezone.utc) + timedelta(hours=2), "type": "access"}, JWT_SECRET, algorithm=JWT_ALG)

def create_refresh_token(user_id: str) -> str:
    return jwt.encode({"sub": user_id, "exp": datetime.now(timezone.utc) + timedelta(days=7), "type": "refresh"}, JWT_SECRET, algorithm=JWT_ALG)

async def get_current_user(request: Request) -> dict:
    token = request.cookies.get("access_token")
    if not token:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALG])
        if payload.get("type") != "access":
            raise HTTPException(status_code=401, detail="Invalid token type")
        user = await db.users.find_one({"_id": ObjectId(payload["sub"])})
        if not user:
            raise HTTPException(status_code=401, detail="User not found")
        user["id"] = str(user.pop("_id"))
        user.pop("password_hash", None)
        return user
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")

async def require_role(request: Request, roles: list) -> dict:
    user = await get_current_user(request)
    if user["role"] not in roles:
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    return user

# ===== WEBSOCKET MANAGER =====
class ConnectionManager:
    def __init__(self):
        self.connections: Dict[str, List[WebSocket]] = {}
    async def connect(self, channel: str, ws: WebSocket):
        await ws.accept()
        self.connections.setdefault(channel, []).append(ws)
    def disconnect(self, channel: str, ws: WebSocket):
        if channel in self.connections:
            self.connections[channel] = [c for c in self.connections[channel] if c != ws]
    async def broadcast(self, channel: str, data: dict):
        dead = []
        for ws in self.connections.get(channel, []):
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(channel, ws)

ws_manager = ConnectionManager()

# ===== UTILS =====
def haversine(lat1, lon1, lat2, lon2):
    lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = sin(dlat/2)**2 + cos(lat1) * cos(lat2) * sin(dlon/2)**2
    return 6371 * 2 * asin(sqrt(a))

def doc_to_dict(doc):
    if doc is None:
        return None
    if "_id" in doc:
        doc["id"] = str(doc.pop("_id"))
    for key in list(doc.keys()):
        if isinstance(doc[key], ObjectId):
            doc[key] = str(doc[key])
        elif isinstance(doc[key], datetime):
            doc[key] = doc[key].isoformat()
    return doc

# ===== PYDANTIC MODELS =====
class RegisterReq(BaseModel):
    email: str
    password: str
    name: str
    role: str = "customer"
    phone: str = ""

class LoginReq(BaseModel):
    email: str
    password: str

class KitchenCreate(BaseModel):
    name: str
    description: str = ""
    address: str = ""
    lat: float = 0.0
    lng: float = 0.0
    cuisine_types: List[str] = []
    image_url: str = ""

class MenuItemCreate(BaseModel):
    name: str
    description: str = ""
    price: float
    category: str = ""
    image_url: str = ""
    is_available: bool = True

class OrderCreate(BaseModel):
    kitchen_id: str
    items: List[dict]
    delivery_address: str
    origin_url: str

class StatusUpdate(BaseModel):
    status: str

# ===== AUTH ROUTES =====
@api.post("/auth/register")
async def register(req: RegisterReq, response: Response):
    email = req.email.lower().strip()
    if await db.users.find_one({"email": email}):
        raise HTTPException(400, "Email already registered")
    if req.role not in ["customer", "kitchen_provider", "delivery_agent"]:
        raise HTTPException(400, "Invalid role")
    user_doc = {
        "email": email, "password_hash": hash_password(req.password),
        "name": req.name, "role": req.role, "phone": req.phone,
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    result = await db.users.insert_one(user_doc)
    uid = str(result.inserted_id)
    access = create_access_token(uid, email)
    refresh = create_refresh_token(uid)
    response.set_cookie("access_token", access, httponly=True, secure=False, samesite="lax", max_age=7200, path="/")
    response.set_cookie("refresh_token", refresh, httponly=True, secure=False, samesite="lax", max_age=604800, path="/")
    return {"id": uid, "email": email, "name": req.name, "role": req.role, "token": access}

@api.post("/auth/login")
async def login(req: LoginReq, response: Response, request: Request):
    email = req.email.lower().strip()
    ip = request.client.host if request.client else "unknown"
    identifier = f"{ip}:{email}"
    attempt = await db.login_attempts.find_one({"identifier": identifier})
    if attempt and attempt.get("count", 0) >= 5:
        last = attempt.get("last_attempt")
        if isinstance(last, str):
            last = datetime.fromisoformat(last)
        if last and datetime.now(timezone.utc) - last < timedelta(minutes=15):
            raise HTTPException(429, "Too many login attempts. Try again in 15 minutes.")
    user = await db.users.find_one({"email": email})
    if not user or not verify_password(req.password, user["password_hash"]):
        await db.login_attempts.update_one(
            {"identifier": identifier},
            {"$inc": {"count": 1}, "$set": {"last_attempt": datetime.now(timezone.utc).isoformat()}},
            upsert=True
        )
        raise HTTPException(401, "Invalid email or password")
    await db.login_attempts.delete_one({"identifier": identifier})
    uid = str(user["_id"])
    access = create_access_token(uid, email)
    refresh = create_refresh_token(uid)
    response.set_cookie("access_token", access, httponly=True, secure=False, samesite="lax", max_age=7200, path="/")
    response.set_cookie("refresh_token", refresh, httponly=True, secure=False, samesite="lax", max_age=604800, path="/")
    return {"id": uid, "email": email, "name": user["name"], "role": user["role"], "token": access}

@api.post("/auth/logout")
async def logout(response: Response):
    response.delete_cookie("access_token", path="/")
    response.delete_cookie("refresh_token", path="/")
    return {"message": "Logged out"}

@api.get("/auth/me")
async def get_me(request: Request):
    return await get_current_user(request)

@api.post("/auth/refresh")
async def refresh(request: Request, response: Response):
    token = request.cookies.get("refresh_token")
    if not token:
        raise HTTPException(401, "No refresh token")
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALG])
        if payload.get("type") != "refresh":
            raise HTTPException(401, "Invalid token type")
        user = await db.users.find_one({"_id": ObjectId(payload["sub"])})
        if not user:
            raise HTTPException(401, "User not found")
        access = create_access_token(str(user["_id"]), user["email"])
        response.set_cookie("access_token", access, httponly=True, secure=False, samesite="lax", max_age=7200, path="/")
        return {"message": "Token refreshed"}
    except jwt.InvalidTokenError:
        raise HTTPException(401, "Invalid token")

# ===== KITCHEN ROUTES =====
@api.get("/kitchens")
async def list_kitchens(lat: Optional[float] = None, lng: Optional[float] = None, search: Optional[str] = None, radius: float = 50):
    query = {}
    if search:
        query["$or"] = [
            {"name": {"$regex": search, "$options": "i"}},
            {"cuisine_types": {"$regex": search, "$options": "i"}}
        ]
    kitchens = await db.kitchens.find(query).to_list(100)
    results = []
    for k in kitchens:
        k = doc_to_dict(k)
        if lat is not None and lng is not None and k.get("lat") and k.get("lng"):
            k["distance"] = round(haversine(lat, lng, k["lat"], k["lng"]), 2)
        else:
            k["distance"] = None
        results.append(k)
    if lat is not None and lng is not None:
        results = [k for k in results if k["distance"] is None or k["distance"] <= radius]
        results.sort(key=lambda x: x["distance"] if x["distance"] is not None else 9999)
    return results

@api.get("/my/kitchen")
async def get_my_kitchen(request: Request):
    user = await require_role(request, ["kitchen_provider"])
    kitchen = await db.kitchens.find_one({"owner_id": user["id"]})
    if not kitchen:
        return None
    return doc_to_dict(kitchen)

@api.get("/kitchens/{kitchen_id}")
async def get_kitchen(kitchen_id: str):
    kitchen = await db.kitchens.find_one({"_id": ObjectId(kitchen_id)})
    if not kitchen:
        raise HTTPException(404, "Kitchen not found")
    return doc_to_dict(kitchen)

@api.post("/kitchens")
async def create_kitchen(req: KitchenCreate, request: Request):
    user = await require_role(request, ["kitchen_provider", "admin"])
    existing = await db.kitchens.find_one({"owner_id": user["id"]})
    if existing:
        raise HTTPException(400, "You already have a kitchen")
    kitchen_doc = {
        **req.model_dump(), "owner_id": user["id"],
        "is_open": True, "rating": 4.5, "total_orders": 0,
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    result = await db.kitchens.insert_one(kitchen_doc)
    kitchen_doc["id"] = str(result.inserted_id)
    kitchen_doc.pop("_id", None)
    return kitchen_doc

@api.put("/kitchens/{kitchen_id}")
async def update_kitchen(kitchen_id: str, req: KitchenCreate, request: Request):
    user = await require_role(request, ["kitchen_provider", "admin"])
    kitchen = await db.kitchens.find_one({"_id": ObjectId(kitchen_id)})
    if not kitchen:
        raise HTTPException(404, "Kitchen not found")
    if str(kitchen.get("owner_id")) != user["id"] and user["role"] != "admin":
        raise HTTPException(403, "Not authorized")
    await db.kitchens.update_one({"_id": ObjectId(kitchen_id)}, {"$set": req.model_dump()})
    return {"message": "Kitchen updated"}

@api.put("/kitchens/{kitchen_id}/toggle")
async def toggle_kitchen(kitchen_id: str, request: Request):
    user = await require_role(request, ["kitchen_provider", "admin"])
    kitchen = await db.kitchens.find_one({"_id": ObjectId(kitchen_id)})
    if not kitchen:
        raise HTTPException(404, "Kitchen not found")
    if str(kitchen.get("owner_id")) != user["id"] and user["role"] != "admin":
        raise HTTPException(403, "Not authorized")
    new_open = not kitchen.get("is_open", True)
    await db.kitchens.update_one({"_id": ObjectId(kitchen_id)}, {"$set": {"is_open": new_open}})
    return {"is_open": new_open}

# ===== MENU ROUTES =====
@api.get("/kitchens/{kitchen_id}/menu")
async def get_menu(kitchen_id: str):
    items = await db.menu_items.find({"kitchen_id": kitchen_id}).to_list(100)
    return [doc_to_dict(i) for i in items]

@api.post("/kitchens/{kitchen_id}/menu")
async def add_menu_item(kitchen_id: str, req: MenuItemCreate, request: Request):
    user = await require_role(request, ["kitchen_provider", "admin"])
    kitchen = await db.kitchens.find_one({"_id": ObjectId(kitchen_id)})
    if not kitchen:
        raise HTTPException(404, "Kitchen not found")
    if str(kitchen.get("owner_id")) != user["id"] and user["role"] != "admin":
        raise HTTPException(403, "Not authorized")
    item_doc = {**req.model_dump(), "kitchen_id": kitchen_id, "created_at": datetime.now(timezone.utc).isoformat()}
    result = await db.menu_items.insert_one(item_doc)
    item_doc["id"] = str(result.inserted_id)
    item_doc.pop("_id", None)
    return item_doc

@api.put("/menu-items/{item_id}")
async def update_menu_item(item_id: str, req: MenuItemCreate, request: Request):
    user = await require_role(request, ["kitchen_provider", "admin"])
    item = await db.menu_items.find_one({"_id": ObjectId(item_id)})
    if not item:
        raise HTTPException(404, "Item not found")
    kitchen = await db.kitchens.find_one({"_id": ObjectId(item["kitchen_id"])})
    if not kitchen or (str(kitchen.get("owner_id")) != user["id"] and user["role"] != "admin"):
        raise HTTPException(403, "Not authorized")
    await db.menu_items.update_one({"_id": ObjectId(item_id)}, {"$set": req.model_dump()})
    return {"message": "Item updated"}

@api.delete("/menu-items/{item_id}")
async def delete_menu_item(item_id: str, request: Request):
    user = await require_role(request, ["kitchen_provider", "admin"])
    item = await db.menu_items.find_one({"_id": ObjectId(item_id)})
    if not item:
        raise HTTPException(404, "Item not found")
    kitchen = await db.kitchens.find_one({"_id": ObjectId(item["kitchen_id"])})
    if not kitchen or (str(kitchen.get("owner_id")) != user["id"] and user["role"] != "admin"):
        raise HTTPException(403, "Not authorized")
    await db.menu_items.delete_one({"_id": ObjectId(item_id)})
    return {"message": "Item deleted"}

# ===== ORDER ROUTES =====
VALID_TRANSITIONS = {
    "placed": ["accepted", "rejected"],
    "accepted": ["preparing"],
    "preparing": ["ready"],
    "ready": ["picked_up"],
    "picked_up": ["delivered"],
}

@api.post("/orders")
async def create_order(req: OrderCreate, request: Request):
    user = await require_role(request, ["customer"])
    kitchen = await db.kitchens.find_one({"_id": ObjectId(req.kitchen_id)})
    if not kitchen:
        raise HTTPException(404, "Kitchen not found")
    if not kitchen.get("is_open", True):
        raise HTTPException(400, "Kitchen is currently closed")
    order_items = []
    total = 0.0
    for item in req.items:
        mi = await db.menu_items.find_one({"_id": ObjectId(item["menu_item_id"])})
        if not mi:
            raise HTTPException(404, f"Menu item not found")
        if not mi.get("is_available", True):
            raise HTTPException(400, f"{mi['name']} is not available")
        qty = max(1, int(item.get("quantity", 1)))
        order_items.append({
            "menu_item_id": str(mi["_id"]), "name": mi["name"],
            "price": mi["price"], "quantity": qty, "image_url": mi.get("image_url", "")
        })
        total += mi["price"] * qty
    total = round(total, 2)
    order_doc = {
        "customer_id": user["id"], "customer_name": user["name"],
        "customer_email": user.get("email", ""),
        "kitchen_id": req.kitchen_id, "kitchen_name": kitchen["name"],
        "items": order_items, "total": total,
        "delivery_address": req.delivery_address,
        "status": "pending_payment", "payment_status": "pending",
        "delivery_agent_id": None, "delivery_agent_name": None,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat()
    }
    result = await db.orders.insert_one(order_doc)
    order_id = str(result.inserted_id)

    # Stripe checkout
    api_key = os.environ.get("STRIPE_API_KEY")
    host_url = str(request.base_url).rstrip("/")
    webhook_url = f"{host_url}/api/webhook/stripe"
    stripe_checkout = StripeCheckout(api_key=api_key, webhook_url=webhook_url)
    success_url = f"{req.origin_url}/payment-success?session_id={{CHECKOUT_SESSION_ID}}"
    cancel_url = f"{req.origin_url}/cart"
    checkout_req = CheckoutSessionRequest(
        amount=float(total), currency="usd",
        success_url=success_url, cancel_url=cancel_url,
        metadata={"order_id": order_id, "customer_email": user.get("email", "")}
    )
    session = await stripe_checkout.create_checkout_session(checkout_req)
    await db.payment_transactions.insert_one({
        "session_id": session.session_id, "order_id": order_id,
        "amount": total, "currency": "usd", "payment_status": "initiated",
        "customer_id": user["id"], "customer_email": user.get("email", ""),
        "created_at": datetime.now(timezone.utc).isoformat()
    })
    return {"checkout_url": session.url, "order_id": order_id, "session_id": session.session_id}

@api.get("/orders")
async def list_orders(request: Request, status: Optional[str] = None):
    user = await get_current_user(request)
    query = {}
    if user["role"] == "customer":
        query["customer_id"] = user["id"]
    elif user["role"] == "kitchen_provider":
        kitchens = await db.kitchens.find({"owner_id": user["id"]}).to_list(100)
        kitchen_ids = [str(k["_id"]) for k in kitchens]
        query["kitchen_id"] = {"$in": kitchen_ids}
    elif user["role"] == "delivery_agent":
        query["delivery_agent_id"] = user["id"]
    if status:
        if status == "active":
            query["status"] = {"$in": ["placed", "accepted", "preparing", "ready", "picked_up"]}
        else:
            query["status"] = status
    orders = await db.orders.find(query).sort("created_at", -1).to_list(200)
    return [doc_to_dict(o) for o in orders]

@api.get("/orders/{order_id}")
async def get_order(order_id: str, request: Request):
    user = await get_current_user(request)
    order = await db.orders.find_one({"_id": ObjectId(order_id)})
    if not order:
        raise HTTPException(404, "Order not found")
    return doc_to_dict(order)

@api.put("/orders/{order_id}/status")
async def update_order_status(order_id: str, req: StatusUpdate, request: Request):
    user = await get_current_user(request)
    order = await db.orders.find_one({"_id": ObjectId(order_id)})
    if not order:
        raise HTTPException(404, "Order not found")
    current = order["status"]
    new_status = req.status
    valid = VALID_TRANSITIONS.get(current, [])
    if new_status not in valid:
        raise HTTPException(400, f"Cannot transition from {current} to {new_status}")
    if new_status in ["accepted", "rejected", "preparing", "ready"]:
        if user["role"] not in ["kitchen_provider", "admin"]:
            raise HTTPException(403, "Only kitchen can update this status")
    if new_status in ["picked_up", "delivered"]:
        if user["role"] not in ["delivery_agent", "admin"]:
            raise HTTPException(403, "Only delivery agent can update this status")
    update_data = {"status": new_status, "updated_at": datetime.now(timezone.utc).isoformat()}
    await db.orders.update_one({"_id": ObjectId(order_id)}, {"$set": update_data})
    await ws_manager.broadcast(f"order:{order_id}", {"status": new_status, "updated_at": update_data["updated_at"]})
    if new_status == "delivered":
        await db.kitchens.update_one({"_id": ObjectId(order["kitchen_id"])}, {"$inc": {"total_orders": 1}})
    return {"message": f"Order updated to {new_status}"}

# ===== PAYMENT ROUTES =====
@api.get("/payments/status/{session_id}")
async def check_payment(session_id: str, request: Request):
    api_key = os.environ.get("STRIPE_API_KEY")
    host_url = str(request.base_url).rstrip("/")
    webhook_url = f"{host_url}/api/webhook/stripe"
    stripe_checkout = StripeCheckout(api_key=api_key, webhook_url=webhook_url)
    status = await stripe_checkout.get_checkout_status(session_id)
    tx = await db.payment_transactions.find_one({"session_id": session_id})
    if tx and tx.get("payment_status") != "paid" and status.payment_status == "paid":
        await db.payment_transactions.update_one(
            {"session_id": session_id},
            {"$set": {"payment_status": "paid", "status": status.status}}
        )
        order_id = tx.get("order_id")
        if order_id:
            await db.orders.update_one(
                {"_id": ObjectId(order_id), "status": "pending_payment"},
                {"$set": {"status": "placed", "payment_status": "paid", "updated_at": datetime.now(timezone.utc).isoformat()}}
            )
            await ws_manager.broadcast(f"order:{order_id}", {"status": "placed"})
    elif tx and status.payment_status != "paid":
        await db.payment_transactions.update_one(
            {"session_id": session_id},
            {"$set": {"payment_status": status.payment_status, "status": status.status}}
        )
    order_id = tx.get("order_id") if tx else None
    return {"payment_status": status.payment_status, "status": status.status, "order_id": order_id}

@app.post("/api/webhook/stripe")
async def stripe_webhook(request: Request):
    api_key = os.environ.get("STRIPE_API_KEY")
    host_url = str(request.base_url).rstrip("/")
    webhook_url = f"{host_url}/api/webhook/stripe"
    stripe_checkout = StripeCheckout(api_key=api_key, webhook_url=webhook_url)
    body = await request.body()
    sig = request.headers.get("Stripe-Signature")
    try:
        event = await stripe_checkout.handle_webhook(body, sig)
        logger.info(f"Webhook: {event.event_type}, session: {event.session_id}")
        if event.payment_status == "paid":
            tx = await db.payment_transactions.find_one({"session_id": event.session_id})
            if tx and tx.get("payment_status") != "paid":
                await db.payment_transactions.update_one(
                    {"session_id": event.session_id},
                    {"$set": {"payment_status": "paid", "status": "complete"}}
                )
                oid = tx.get("order_id")
                if oid:
                    await db.orders.update_one(
                        {"_id": ObjectId(oid), "status": "pending_payment"},
                        {"$set": {"status": "placed", "payment_status": "paid"}}
                    )
    except Exception as e:
        logger.error(f"Webhook error: {e}")
    return {"status": "ok"}

# ===== DELIVERY ROUTES =====
@api.get("/delivery/orders")
async def delivery_orders(request: Request):
    user = await require_role(request, ["delivery_agent", "admin"])
    orders = await db.orders.find({"delivery_agent_id": user["id"]}).sort("created_at", -1).to_list(100)
    return [doc_to_dict(o) for o in orders]

@api.get("/delivery/available")
async def available_deliveries(request: Request):
    await require_role(request, ["delivery_agent", "admin"])
    orders = await db.orders.find({
        "status": "ready",
        "$or": [{"delivery_agent_id": None}, {"delivery_agent_id": ""}]
    }).to_list(100)
    return [doc_to_dict(o) for o in orders]

@api.put("/delivery/orders/{order_id}/accept")
async def accept_delivery(order_id: str, request: Request):
    user = await require_role(request, ["delivery_agent"])
    order = await db.orders.find_one({"_id": ObjectId(order_id)})
    if not order:
        raise HTTPException(404, "Order not found")
    if order["status"] != "ready":
        raise HTTPException(400, "Order not ready for pickup")
    await db.orders.update_one(
        {"_id": ObjectId(order_id)},
        {"$set": {"delivery_agent_id": user["id"], "delivery_agent_name": user["name"]}}
    )
    return {"message": "Delivery accepted"}

# ===== ADMIN ROUTES =====
@api.get("/admin/users")
async def admin_users(request: Request, role: Optional[str] = None):
    await require_role(request, ["admin"])
    query = {"role": role} if role else {}
    users = await db.users.find(query, {"password_hash": 0}).to_list(1000)
    return [doc_to_dict(u) for u in users]

@api.get("/admin/analytics")
async def admin_analytics(request: Request):
    await require_role(request, ["admin"])
    total_orders = await db.orders.count_documents({})
    total_users = await db.users.count_documents({})
    total_kitchens = await db.kitchens.count_documents({})
    total_agents = await db.users.count_documents({"role": "delivery_agent"})
    pipeline = [{"$match": {"payment_status": "paid"}}, {"$group": {"_id": None, "total": {"$sum": "$total"}}}]
    rev = await db.orders.aggregate(pipeline).to_list(1)
    total_revenue = rev[0]["total"] if rev else 0
    status_pipeline = [{"$group": {"_id": "$status", "count": {"$sum": 1}}}]
    statuses = await db.orders.aggregate(status_pipeline).to_list(20)
    orders_by_status = {s["_id"]: s["count"] for s in statuses}
    recent = await db.orders.find().sort("created_at", -1).to_list(10)
    return {
        "total_orders": total_orders, "total_users": total_users,
        "total_kitchens": total_kitchens, "total_agents": total_agents,
        "total_revenue": round(total_revenue, 2), "orders_by_status": orders_by_status,
        "recent_orders": [doc_to_dict(o) for o in recent]
    }

@api.put("/admin/orders/{order_id}/assign")
async def assign_delivery(order_id: str, request: Request, agent_id: str = Body(..., embed=True)):
    await require_role(request, ["admin"])
    order = await db.orders.find_one({"_id": ObjectId(order_id)})
    if not order:
        raise HTTPException(404, "Order not found")
    agent = await db.users.find_one({"_id": ObjectId(agent_id), "role": "delivery_agent"})
    if not agent:
        raise HTTPException(404, "Delivery agent not found")
    await db.orders.update_one(
        {"_id": ObjectId(order_id)},
        {"$set": {"delivery_agent_id": str(agent["_id"]), "delivery_agent_name": agent["name"]}}
    )
    return {"message": "Delivery agent assigned"}

# ===== WEBSOCKET =====
@app.websocket("/api/ws/orders/{order_id}")
async def ws_orders(websocket: WebSocket, order_id: str):
    await ws_manager.connect(f"order:{order_id}", websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        ws_manager.disconnect(f"order:{order_id}", websocket)

# Include router
app.include_router(api)

# ===== STARTUP =====
FOOD_IMAGES = [
    "https://images.unsplash.com/photo-1476005484258-bd38fa5bc155?w=400",
    "https://images.unsplash.com/photo-1764015939108-7963106fa73b?w=400",
    "https://images.unsplash.com/photo-1774106425926-bdbab1356790?w=400",
    "https://images.unsplash.com/photo-1743409390921-1d1e673bc351?w=400",
]

SEED_DATA = [
    {
        "email": "amma@kitchen.com", "name": "Amma", "password": "kitchen123",
        "kitchen": {
            "name": "Amma's Kitchen", "description": "Authentic South Indian home cooking with love",
            "address": "123 MG Road, Downtown", "lat": 40.7580, "lng": -73.9855,
            "cuisine_types": ["South Indian", "Vegetarian"],
            "image_url": "https://images.unsplash.com/photo-1622021142947-da7dedc7c39a?w=600"
        },
        "items": [
            {"name": "Masala Dosa", "description": "Crispy rice crepe with spiced potato filling", "price": 8.99, "category": "Main Course"},
            {"name": "Idli Sambar", "description": "Steamed rice cakes with lentil stew", "price": 6.99, "category": "Main Course"},
            {"name": "Chicken Biryani", "description": "Fragrant basmati rice with spiced chicken", "price": 12.99, "category": "Main Course"},
            {"name": "Filter Coffee", "description": "Traditional South Indian filter coffee", "price": 3.49, "category": "Beverages"},
        ]
    },
    {
        "email": "spice@kitchen.com", "name": "Chef Raj", "password": "kitchen123",
        "kitchen": {
            "name": "The Spice Route", "description": "Bold North Indian flavors from a home kitchen",
            "address": "456 Park Avenue, Midtown", "lat": 40.7614, "lng": -73.9776,
            "cuisine_types": ["North Indian", "Mughlai"],
            "image_url": "https://images.pexels.com/photos/4590935/pexels-photo-4590935.jpeg?w=600"
        },
        "items": [
            {"name": "Butter Chicken", "description": "Tender chicken in rich tomato-butter sauce", "price": 13.99, "category": "Main Course"},
            {"name": "Garlic Naan", "description": "Fresh baked flatbread with garlic butter", "price": 3.99, "category": "Breads"},
            {"name": "Dal Makhani", "description": "Creamy black lentils slow-cooked overnight", "price": 9.99, "category": "Main Course"},
            {"name": "Mango Lassi", "description": "Chilled yogurt drink with fresh mango", "price": 4.49, "category": "Beverages"},
        ]
    },
    {
        "email": "green@kitchen.com", "name": "Maya", "password": "kitchen123",
        "kitchen": {
            "name": "Green Bowl", "description": "Fresh, healthy bowls and smoothies made daily",
            "address": "789 Broadway, SoHo", "lat": 40.7234, "lng": -73.9987,
            "cuisine_types": ["Healthy", "Salads", "Vegan"],
            "image_url": "https://images.unsplash.com/photo-1476005484258-bd38fa5bc155?w=600"
        },
        "items": [
            {"name": "Quinoa Power Bowl", "description": "Quinoa with roasted veggies and tahini", "price": 11.99, "category": "Bowls"},
            {"name": "Caesar Salad", "description": "Romaine, croutons, parmesan, house dressing", "price": 9.49, "category": "Salads"},
            {"name": "Acai Smoothie Bowl", "description": "Acai blend topped with granola and berries", "price": 10.99, "category": "Bowls"},
            {"name": "Green Detox Juice", "description": "Spinach, apple, ginger, lemon", "price": 5.99, "category": "Beverages"},
        ]
    }
]

@app.on_event("startup")
async def startup():
    await db.users.create_index("email", unique=True)
    await db.kitchens.create_index("owner_id")
    await db.menu_items.create_index("kitchen_id")
    await db.orders.create_index("customer_id")
    await db.orders.create_index("kitchen_id")
    await db.payment_transactions.create_index("session_id", unique=True)
    await db.login_attempts.create_index("identifier")

    # Seed admin
    admin_email = os.environ.get("ADMIN_EMAIL", "admin@example.com")
    admin_password = os.environ.get("ADMIN_PASSWORD", "admin123")
    existing_admin = await db.users.find_one({"email": admin_email})
    if not existing_admin:
        await db.users.insert_one({
            "email": admin_email, "password_hash": hash_password(admin_password),
            "name": "Admin", "role": "admin", "phone": "",
            "created_at": datetime.now(timezone.utc).isoformat()
        })
        logger.info("Admin user seeded")
    elif not verify_password(admin_password, existing_admin["password_hash"]):
        await db.users.update_one({"email": admin_email}, {"$set": {"password_hash": hash_password(admin_password)}})

    # Seed kitchens
    for idx, data in enumerate(SEED_DATA):
        if not await db.users.find_one({"email": data["email"]}):
            result = await db.users.insert_one({
                "email": data["email"], "password_hash": hash_password(data["password"]),
                "name": data["name"], "role": "kitchen_provider", "phone": "",
                "created_at": datetime.now(timezone.utc).isoformat()
            })
            owner_id = str(result.inserted_id)
            kitchen_doc = {**data["kitchen"], "owner_id": owner_id, "is_open": True, "rating": 4.2 + idx * 0.2, "total_orders": 10 + idx * 5, "created_at": datetime.now(timezone.utc).isoformat()}
            k_result = await db.kitchens.insert_one(kitchen_doc)
            kitchen_id = str(k_result.inserted_id)
            for i, item in enumerate(data["items"]):
                await db.menu_items.insert_one({
                    **item, "kitchen_id": kitchen_id, "image_url": FOOD_IMAGES[i % len(FOOD_IMAGES)],
                    "is_available": True, "created_at": datetime.now(timezone.utc).isoformat()
                })
            logger.info(f"Seeded kitchen: {data['kitchen']['name']}")

    # Seed delivery agents
    for d in [{"email": "driver1@delivery.com", "name": "Alex Driver"}, {"email": "driver2@delivery.com", "name": "Sam Courier"}]:
        if not await db.users.find_one({"email": d["email"]}):
            await db.users.insert_one({
                "email": d["email"], "password_hash": hash_password("delivery123"),
                "name": d["name"], "role": "delivery_agent", "phone": "",
                "created_at": datetime.now(timezone.utc).isoformat()
            })
            logger.info(f"Seeded delivery agent: {d['name']}")

    # Write test credentials
    os.makedirs("/app/memory", exist_ok=True)
    with open("/app/memory/test_credentials.md", "w") as f:
        f.write("# Test Credentials\n\n")
        f.write(f"## Admin\n- Email: {admin_email}\n- Password: {admin_password}\n- Role: admin\n\n")
        f.write("## Kitchen Providers\n")
        for d in SEED_DATA:
            f.write(f"- Email: {d['email']}\n- Password: {d['password']}\n- Role: kitchen_provider\n\n")
        f.write("## Delivery Agents\n- Email: driver1@delivery.com\n- Password: delivery123\n- Role: delivery_agent\n\n")
        f.write("- Email: driver2@delivery.com\n- Password: delivery123\n- Role: delivery_agent\n\n")
        f.write("## Auth Endpoints\n- POST /api/auth/register\n- POST /api/auth/login\n- POST /api/auth/logout\n- GET /api/auth/me\n")
    logger.info("Startup complete")

@app.on_event("shutdown")
async def shutdown():
    client.close()
