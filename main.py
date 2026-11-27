import asyncio, base64, binascii, hashlib, io, json, os, random, secrets, threading, time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional
import qrcode, uvicorn
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from xrpl.asyncio.clients import AsyncJsonRpcClient
from xrpl.asyncio.transaction import autofill as async_autofill, submit_and_wait as async_submit_and_wait
from xrpl.core.binarycodec import encode_for_multisigning
from xrpl.core.keypairs import sign as kp_sign
from xrpl.models.amounts import IssuedCurrencyAmount
from xrpl.models.requests import AccountInfo
from xrpl.models.transactions import (
    AccountSet, AccountSetAsfFlag, EscrowCreate, EscrowFinish, Memo, Payment,
    SignerEntry, SignerListSet, TrustSet, TrustSetFlag,
)
from xrpl.transaction import multisign
from xrpl.utils import datetime_to_ripple_time, xrp_to_drops
from xrpl.wallet import Wallet

load_dotenv()
PORT = int(os.getenv("PORT", 3000))
APP_URL = os.getenv("APP_URL", f"http://localhost:{PORT}").rstrip("/")
ADMIN_MASTER_PIN = os.getenv("ADMIN_MASTER_PIN", "CREST2026")
CORPORATE_FEE_WALLET = os.getenv("CORPORATE_FEE_WALLET", "rCrestVaultAfricaCorporateVault1111111")
XRPL_MASTER_SEED = os.getenv("XRPL_MASTER_SEED", "sEd7SWBibuQTqXKN7YYWmRQVzz75XSd").strip()
XRPL_RPC = "https://rippletest.net"
DB_FILE = "crestvault_clean_db.json"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = FastAPI(title="Crest Vault Africa")
os.makedirs(os.path.join(BASE_DIR, "public"), exist_ok=True)
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "public")), name="static")
_db_lock = threading.RLock()
_xrpl_lock = asyncio.Lock()

def to_hex_code(s: str) -> str:
    c = s.strip().upper()
    return c if len(c) == 3 else binascii.hexlify(c.encode()).decode().upper().ljust(40, "0")

def hex_str(t: str) -> str:
    return binascii.hexlify(t.encode("utf-8")).decode("utf-8").upper()

STABLECOINS = {
    "RLUSD": {"name": "Ripple USD", "code": to_hex_code("RLUSD"), "mainnet": "rMxCKbEDwqr76QuheSUMdEGf4B9xJ8m5De"},
    "USDC":  {"name": "Circle USDC", "code": to_hex_code("USDC"), "mainnet": "rGm7WCVp9gb4jZHWTEtGUr4dd74z2XuWhE"},
    "USDQ":  {"name": "Quantoz USDQ", "code": to_hex_code("USDQ"), "mainnet": "rDk1xiArDMjDqnrR2yWypwQAKg4mKnQYvs"},
    "EURCV": {"name": "SG-FORGE EURCV", "code": to_hex_code("EURCV"), "mainnet": "rUNaS5sqRuxZz6V7rBGhoSaZiVYA3ut4UL"},
    "XSGD":  {"name": "StraitsX XSGD", "code": to_hex_code("XSGD"), "mainnet": "rK67JczCpaYXVtfw3qJVmqwpSfa1bYTptw"},
}

COUNTRIES = {
    "UG": {"name": "Uganda", "currency": "UGX", "vat": 0.18, "levy": 0.015},
    "KE": {"name": "Kenya", "currency": "KES", "vat": 0.16, "levy": 0.015},
    "NG": {"name": "Nigeria", "currency": "NGN", "vat": 0.075, "levy": 0.01},
    "ZA": {"name": "South Africa", "currency": "ZAR", "vat": 0.15, "levy": 0.0},
    "RW": {"name": "Rwanda", "currency": "RWF", "vat": 0.18, "levy": 0.015},
    "GH": {"name": "Ghana", "currency": "GHS", "vat": 0.15, "levy": 0.01},
    "AE": {"name": "UAE", "currency": "AED", "vat": 0.05, "levy": 0.0},
}
RATES = {"USD": 1.0, "UGX": 3700.0, "KES": 129.0, "NGN": 1580.0, "ZAR": 17.8, "RWF": 1365.0, "GHS": 15.6, "AED": 3.67}
ROLES = {
    "IMPORTER": "Importer / Kikuubo Trader", "EXPORTER": "Factory / Agro-Exporter",
    "CARRIER": "Freight Carrier / Fleet Owner", "WHOLESALER": "Wholesaler / Distributor",
    "RETAILER": "Retail Shop / Pharmacy", "AUDITOR": "External CPA / Tax Accountant",
    "REGULATOR": "Government Inspector (URA / UNBS / NDA)", "SUPER_ADMIN": "Level 6 Platform Owner / Super-Admin",
}
DEFAULT_PRICING = {"retailProUsd": 15.0, "proTradeUsd": 49.0, "factoryEnterpriseUsd": 199.0, "offrampSpreadPct": 0.40, "atomicSettlementFeePct": 0.20, "importerEscrowFlatUsd": 35.0}

class SignupReq(BaseModel):
    countryCode: str; userRole: str; planTier: str = "PRO_TRADE"; userName: str; orgName: str
    taxIdTin: Optional[str] = ""; permitNumber: Optional[str] = ""; truckPlate: Optional[str] = ""; adminPin: str = ""

class DealReq(BaseModel):
    countryCode: str = "UG"; settlementCoin: str = "RLUSD"; incoterm: str = "FOB"
    supplierName: str; supplierBankName: str = "Bank of China"; supplierAccountNumber: str = "621700123456789"
    productDesc: str; goodsUsd: float; freightUsd: float = 1800.0; insuranceUsd: float = 150.0
    splitPct1: int = 30; splitPct2: int = 70

class BidReq(BaseModel):
    dealId: str; truckPlate: str; rectsSealId: str; bidUsd: float

class InvReq(BaseModel):
    countryCode: str = "UG"; flowType: str = "EXPORT"; buyerName: str; productDesc: str; amountUsd: float

class BatchReq(BaseModel):
    countryCode: str = "UG"; agency: str = "UNBS"; permitNo: str; batchCode: str; productName: str

class RetailReq(BaseModel):
    countryCode: str = "UG"; entryType: str; itemDesc: str; localAmount: float; isCredit14Day: bool = False

class AdminReq(BaseModel):
    pricing: dict; sockets: dict = {}

def sha256_hex(d: Any) -> str:
    return hashlib.sha256(json.dumps(d, sort_keys=True).encode()).hexdigest().upper()

def load_db() -> Dict[str, Any]:
    base = {"actors": {}, "sessions": {}, "escrowDeals": [], "invoices": [], "freightBids": [], "batches": [], "serialQRs": [], "taxLedger": [], "corporateRevenue": [], "pricing": DEFAULT_PRICING.copy(), "sockets": {}}
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r") as f: base.update(json.load(f))
        except Exception: pass
    return base

def save_db(d: Dict[str, Any]):
    with _db_lock:
        with open(DB_FILE, "w") as f: json.dump(d, f, indent=2)

def get_session(x_session_token: Optional[str] = Header(default=None)):
    s = load_db()["sessions"].get(x_session_token or "")
    if not s: raise HTTPException(status_code=401, detail="Session expired")
    return s

# --- CLEAN 3-ACCOUNT XRPL ESCROW ENGINE (ISSUER -> BUYER -> OFFRAMP) ---
async def get_3_actors(client: AsyncJsonRpcClient, code: str) -> Dict[str, Any]:
    db = load_db()
    if code in db["actors"]:
        a = db["actors"][code]
        return {"issuer": Wallet.from_seed(a["issuer"]), "buyer": Wallet.from_seed(a["buyer"]), "offramp": Wallet.from_seed(a["offramp"])}

    master = Wallet.from_seed(XRPL_MASTER_SEED)
    issuer, buyer, offramp = Wallet.create(), Wallet.create(), Wallet.create()
    for w, amt in [(issuer, 12), (buyer, 15), (offramp, 12)]:
        await async_submit_and_wait(Payment(account=master.address, destination=w.address, amount=xrp_to_drops(amt)), client, master)
    await async_submit_and_wait(AccountSet(account=issuer.address, set_flag=17), client, issuer)
    for w in (buyer, offramp):
        await async_submit_and_wait(TrustSet(account=w.address, limit_amount=IssuedCurrencyAmount(currency=code, issuer=issuer.address, value="100000000"), flags=TrustSetFlag.TF_SET_NO_RIPPLE), client, w)
    await async_submit_and_wait(Payment(account=issuer.address, destination=buyer.address, amount=IssuedCurrencyAmount(currency=code, issuer=issuer.address, value="50000000")), client, issuer)

    db["actors"][code] = {"issuer": issuer.seed, "buyer": buyer.seed, "offramp": offramp.seed}
    save_db(db)
    return {"issuer": issuer, "buyer": buyer, "offramp": offramp}

@app.get("/")
def home():
    return FileResponse(os.path.join(BASE_DIR, "public", "index.html"))

@app.get("/api/health")
def health():
    return {"status": "ONLINE", "ugx_per_usd": RATES["UGX"], "coins": list(STABLECOINS.keys())}

@app.post("/api/auth/signup")
def signup(req: SignupReq):
    c = req.countryCode.strip().upper()
    if c not in COUNTRIES: raise HTTPException(status_code=403, detail="Jurisdiction blocked or restricted")
    if req.userRole == "SUPER_ADMIN" and req.adminPin != ADMIN_MASTER_PIN: raise HTTPException(status_code=403, detail="Invalid PIN")
    db = load_db()
    p = COUNTRIES[c]
    acct = {"countryCode": c, "countryName": p["name"], "currency": p["currency"], "userRole": req.userRole, "roleTitle": ROLES.get(req.userRole, req.userRole), "userName": req.userName, "orgName": req.orgName, "taxIdTin": req.taxIdTin, "clearingAccountRef": f"CVA-{c}-{secrets.token_hex(5).upper()}"}
    tok = secrets.token_hex(16)
    db["sessions"][tok] = acct
    save_db(db)
    return {"success": True, "token": tok, "session": acct}

@app.get("/api/workspace")
def workspace(s: dict = Depends(get_session)):
    db = load_db()
    c = s["countryCode"]
    adm = s["userRole"] == "SUPER_ADMIN"
    return {
        "session": s, "fxRate": RATES[COUNTRIES[c]["currency"]], "corporateFeeWallet": CORPORATE_FEE_WALLET,
        "pricing": db["pricing"], "sockets": db["sockets"],
        "escrowDeals": db["escrowDeals"] if adm else [x for x in db["escrowDeals"] if x["countryCode"] == c],
        "invoices": db["invoices"] if adm else [x for x in db["invoices"] if x["countryCode"] == c],
        "freightBids": db["freightBids"] if adm else [x for x in db["freightBids"] if x["countryCode"] == c],
        "batches": db["batches"] if adm else [x for x in db["batches"] if x["countryCode"] == c],
        "taxLedger": db["taxLedger"] if adm else [x for x in db["taxLedger"] if x["countryCode"] == c],
        "corporateRevenue": db["corporateRevenue"] if adm else [],
    }

@app.post("/api/deals")
async def create_deal(req: DealReq, s: dict = Depends(get_session)):
    if req.splitPct1 + req.splitPct2 != 100: raise HTTPException(status_code=400, detail="Splits must equal 100%")
    tok = STABLECOINS.get(req.settlementCoin.upper(), STABLECOINS["RLUSD"])
    c = s["countryCode"]
    fx = RATES[COUNTRIES[c]["currency"]]
    did = f"ESC-{c}-{str(int(time.time()*1000))[-5:]}"
    memo = Memo(memo_type=hex_str("OFFRAMP_SETTLEMENT"), memo_data=hex_str(f"INV:{did}|BANK:{req.supplierBankName}|ACCT:{req.supplierAccountNumber}"))

    async with _xrpl_lock:
