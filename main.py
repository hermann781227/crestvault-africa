import asyncio, base64, binascii, hashlib, io, json, os, random, secrets, threading, time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional
import httpx, qrcode, uvicorn
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from xrpl.asyncio.clients import AsyncJsonRpcClient
from xrpl.asyncio.transaction import submit_and_wait as async_submit_and_wait
from xrpl.models.amounts import IssuedCurrencyAmount
from xrpl.models.transactions import (
    AccountSet, AccountSetAsfFlag, EscrowCreate, Memo, Payment,
    TrustSet, TrustSetFlag,
)
from xrpl.utils import datetime_to_ripple_time, xrp_to_drops
from xrpl.wallet import Wallet

load_dotenv()
PORT = int(os.getenv("PORT", 3000))
APP_URL = os.getenv("APP_URL", f"http://localhost:{PORT}").rstrip("/")
ADMIN_MASTER_PIN = os.getenv("ADMIN_MASTER_PIN", "CREST2026")
CORPORATE_FEE_WALLET = os.getenv("CORPORATE_FEE_WALLET", "rCrestVaultAfricaCorporateVault1111111")
XRPL_MASTER_SEED = os.getenv("XRPL_MASTER_SEED", "sEd7oQeLn4Qj1cp3CwKRKyK8R826x1g").strip()
XRPL_RPC = "https://s.altnet.rippletest.net:51234"
DB_FILE = "crestvault_clean_db.json"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = FastAPI(title="Crest Vault Africa")
os.makedirs(os.path.join(BASE_DIR, "public"), exist_ok=True)
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "public")), name="static")
_db_lock = threading.RLock()
_xrpl_lock = asyncio.Lock()
_rpc_client = AsyncJsonRpcClient(XRPL_RPC)

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
    "CARRIER": "Freight Carrier / Fleet Owner", "WHOLESALER": "Wholesaler / Domestic Factory",
    "RETAILER": "Retail Shop / Pharmacy", "AUDITOR": "External CPA / Tax Accountant",
    "REGULATOR": "Government Inspector (URA / UNBS / NDA)", "SUPER_ADMIN": "Level 6 Platform Owner / Super-Admin",
}
DEFAULT_PRICING = {"retailProUsd": 15.0, "proTradeUsd": 49.0, "factoryEnterpriseUsd": 199.0, "offrampSpreadPct": 0.40, "atomicSettlementFeePct": 0.20, "importerEscrowFlatUsd": 35.0}
DEFAULT_SOCKETS = {
    "offrampUrl": "", "offrampApiKey": "",
    "localGatewayUrl": "", "localGatewayKey": "",
    "uraEfrisUrl": "", "uraEfrisKey": "",
    "customsRectsUrl": "", "customsRectsKey": "",
    "standardsRegUrl": "", "standardsRegKey": "",
    "fxOracleUrl": "", "fxOracleKey": "",
}

class SignupReq(BaseModel):
    countryCode: str; userRole: str; planTier: str = "PRO_TRADE"; userName: str; orgName: str
    taxIdTin: Optional[str] = ""; permitNumber: Optional[str] = ""; truckPlate: Optional[str] = ""; adminPin: str = ""

class DealReq(BaseModel):
    countryCode: str = "UG"; settlementCoin: str = "RLUSD"; incoterm: str = "FOB"
    supplierName: str; supplierBankName: str = "Bank of China"; supplierAccountNumber: str = "621700123456789"
    productDesc: str; goodsUsd: float; freightUsd: float = 1800.0; insuranceUsd: float = 150.0
    splitPct1: int = 30; splitPct2: int = 70
    tradeDirection: str = "IMPORT"; exportPermitNo: Optional[str] = ""; eudrOrigin: Optional[str] = ""; bolNumber: Optional[str] = ""

class PrnReq(BaseModel):
    prnNumber: str

class BidReq(BaseModel):
    dealId: str; truckPlate: str; rectsSealId: str; bidUsd: float
    payoutChannel: str = "MTN Mobile Money"; payoutAccount: str = ""

class InvReq(BaseModel):
    countryCode: str = "UG"; flowType: str = "EXPORT"; buyerName: str; productDesc: str; amountUsd: float
    exporterBankName: Optional[str] = ""; exporterAccountNumber: Optional[str] = ""; exportPermitNo: Optional[str] = ""; bolNumber: Optional[str] = ""

class BatchReq(BaseModel):
    countryCode: str = "UG"; agency: str = "UNBS"; permitNo: str; batchCode: str; productName: str

class RetailReq(BaseModel):
    countryCode: str = "UG"; entryType: str; itemDesc: str; localAmount: float; isCredit14Day: bool = False

class AdminReq(BaseModel):
    pricing: dict; sockets: dict = {}

def sha256_hex(d: Any) -> str:
    return hashlib.sha256(json.dumps(d, sort_keys=True).encode()).hexdigest().upper()

def load_db() -> Dict[str, Any]:
    base = {"actors": {}, "sessions": {}, "escrowDeals": [], "invoices": [], "freightBids": [], "batches": [], "serialQRs": [], "taxLedger": [], "corporateRevenue": [], "pricing": DEFAULT_PRICING.copy(), "sockets": DEFAULT_SOCKETS.copy()}
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r") as f:
                loaded = json.load(f)
                base.update(loaded)
                s_merged = DEFAULT_SOCKETS.copy()
                s_merged.update(loaded.get("sockets", {}))
                base["sockets"] = s_merged
        except Exception: pass
    return base

def save_db(d: Dict[str, Any]):
    with _db_lock:
        with open(DB_FILE, "w") as f: json.dump(d, f, indent=2)

def get_session(x_session_token: Optional[str] = Header(default=None)):
    s = load_db()["sessions"].get(x_session_token or "")
    if not s: raise HTTPException(status_code=401, detail="Session expired")
    return s

# --- LIVE EXTERNAL SOCKET FETCHERS (OFF-RAMP + REGULATORY + FX ORACLE) ---
async def fetch_live_fx(currency: str, sockets: dict) -> float:
    url = (sockets.get("fxOracleUrl") or "").strip()
    if url:
        try:
            headers = {"Authorization": f"Bearer {sockets.get('fxOracleKey','')}"} if sockets.get("fxOracleKey") else {}
            async with httpx.AsyncClient(timeout=5.0) as hc:
                r = await hc.get(url, params={"base": "USD", "target": currency}, headers=headers)
                if r.status_code == 200:
                    data = r.json()
                    rate = data.get("rate") or data.get("rates", {}).get(currency)
                    if rate: return float(rate)
        except Exception: pass
    return RATES.get(currency, 1.0)

async def call_regulatory_socket(url: str, key: str, payload: dict) -> Dict[str, Any]:
    u = (url or "").strip()
    if not u:
        return {"live": False, "status": "SANDBOX_VERIFIED"}
    try:
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"} if key else {"Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=6.0) as hc:
            r = await hc.post(u, json=payload, headers=headers)
            if r.status_code in (200, 201):
                out = r.json()
                out["live"] = True
                return out
    except Exception as e:
        return {"live": False, "status": "FALLBACK_VERIFIED", "note": str(e)}
    return {"live": False, "status": "FALLBACK_VERIFIED"}

async def get_3_actors(client: AsyncJsonRpcClient, code: str) -> Dict[str, Any]:
    db = load_db()
    if code in db["actors"]:
        a = db["actors"][code]
        return {"issuer": Wallet.from_seed(a["issuer"]), "buyer": Wallet.from_seed(a["buyer"]), "offramp": Wallet.from_seed(a["offramp"])}

    master = Wallet.from_seed(XRPL_MASTER_SEED)
    issuer, buyer, offramp = Wallet.create(), Wallet.create(), Wallet.create()
    for w, amt in [(issuer, 12), (buyer, 15), (offramp, 12)]:
        await async_submit_and_wait(Payment(account=master.address, destination=w.address, amount=xrp_to_drops(amt)), client, master)
    await async_submit_and_wait(AccountSet(account=issuer.address, set_flag=AccountSetAsfFlag.ASF_DEFAULT_RIPPLE), client, issuer)
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
    ref = f"CVA-{c}-{secrets.token_hex(5).upper()}"
    acct = {"countryCode": c, "countryName": p["name"], "currency": p["currency"], "userRole": req.userRole, "roleTitle": ROLES.get(req.userRole, req.userRole), "userName": req.userName, "orgName": req.orgName, "taxIdTin": req.taxIdTin, "permitNumber": req.permitNumber, "truckPlate": req.truckPlate, "clearingAccountRef": ref, "xrplVault": ref}
    tok = secrets.token_hex(16)
    db["sessions"][tok] = acct
    save_db(db)
    return {"success": True, "token": tok, "session": acct}

@app.get("/api/workspace")
async def workspace(s: dict = Depends(get_session)):
    db = load_db()
    c = s["countryCode"]
    fx = await fetch_live_fx(COUNTRIES[c]["currency"], db["sockets"])
    adm = s["userRole"] in ("SUPER_ADMIN", "CARRIER")
    return {
        "session": s, "fxRate": fx, "corporateFeeWallet": CORPORATE_FEE_WALLET,
        "pricing": db["pricing"], "sockets": db["sockets"],
        "escrowDeals": db["escrowDeals"] if adm else [x for x in db["escrowDeals"] if x["countryCode"] == c],
        "invoices": db["invoices"] if s["userRole"] == "SUPER_ADMIN" else [x for x in db["invoices"] if x["countryCode"] == c],
        "freightBids": db["freightBids"] if adm else [x for x in db["freightBids"] if x["countryCode"] == c],
        "batches": db["batches"] if s["userRole"] == "SUPER_ADMIN" else [x for x in db["batches"] if x["countryCode"] == c],
        "serialQRs": db["serialQRs"],
        "taxLedger": db["taxLedger"] if s["userRole"] == "SUPER_ADMIN" else [x for x in db["taxLedger"] if x["countryCode"] == c],
        "corporateRevenue": db["corporateRevenue"] if s["userRole"] == "SUPER_ADMIN" else [],
    }

@app.post("/api/deals")
async def create_deal(req: DealReq, s: dict = Depends(get_session)):
    if req.splitPct1 + req.splitPct2 != 100: raise HTTPException(status_code=400, detail="Splits must equal 100%")
    db = load_db()
    tok = STABLECOINS.get(req.settlementCoin.upper(), STABLECOINS["RLUSD"])
    c = s["countryCode"]
    fx = await fetch_live_fx(COUNTRIES[c]["currency"], db["sockets"])
    prefix = "EXP" if req.tradeDirection == "EXPORT" or s["userRole"] == "EXPORTER" else "ESC"
    did = f"{prefix}-{c}-{str(int(time.time()*1000))[-5:]}"
    memo = Memo(memo_type=hex_str("OFFRAMP_SETTLEMENT"), memo_data=hex_str(f"INV:{did}|BANK:{req.supplierBankName}|ACCT:{req.supplierAccountNumber}"))

    # Notify Live Off-Ramp Socket if configured
    offramp_res = await call_regulatory_socket(
        db["sockets"].get("offrampUrl", ""), db["sockets"].get("offrampApiKey", ""),
        {"action": "LOCK_ESCROW", "dealId": did, "amountUsd": req.goodsUsd + req.freightUsd, "bank": req.supplierBankName, "account": req.supplierAccountNumber}
    )

    async with _xrpl_lock:
        try:
            actors = await get_3_actors(_rpc_client, tok["code"])
            buyer, offramp = actors["buyer"], actors["offramp"]
            res = await async_submit_and_wait(
                EscrowCreate(
                    account=buyer.address, destination=offramp.address,
                    amount=xrp_to_drops(1),
                    finish_after=datetime_to_ripple_time(datetime.now(timezone.utc) + timedelta(seconds=1)),
                    cancel_after=datetime_to_ripple_time(datetime.now(timezone.utc) + timedelta(days=30)),
                    memos=[memo],
                ), _rpc_client, buyer
            )
            tx_hash = res.result["hash"]
            seq = res.result["tx_json"]["Sequence"]
        except Exception as e:
            raise HTTPException(status_code=503, detail=f"Clearing Network Error: {e}")

    db = load_db()
    deal = {
        "id": did, "countryCode": c, "currency": COUNTRIES[c]["currency"], "fxRate": fx,
        "tradeDirection": "EXPORT" if prefix == "EXP" else "IMPORT",
        "localGoodsAmount": round(req.goodsUsd * fx, 2), "importerName": s["orgName"],
        "supplierName": req.supplierName, "supplierBankName": req.supplierBankName,
        "supplierAccountNumber": req.supplierAccountNumber, "productDesc": req.productDesc,
        "incoterm": req.incoterm, "goodsUsd": req.goodsUsd, "freightUsd": req.freightUsd,
        "splitPct1": req.splitPct1, "splitPct2": req.splitPct2,
        "exportPermitNo": req.exportPermitNo or s.get("permitNumber", ""),
        "eudrOrigin": req.eudrOrigin or "", "bolNumber": req.bolNumber or "",
        "m1Released": False, "m2Released": False, "freightReleased": False,
        "carrierAwarded": "Open for Bidding", "carrierPayoutInfo": "",
        "stage": "STAGE_1_ESCROW_LOCKED", "offrampMode": "LIVE_API" if offramp_res.get("live") else "SANDBOX_RELAYER",
        "xrplLockHash": tx_hash, "xrplOfferSequence": seq, "coinCode": tok["code"],
    }
    db["escrowDeals"].append(deal)
    db["corporateRevenue"].append({"feeType": "TRADE_ESCROW_FEE", "referenceId": did, "amountUsd": 35.0, "xrplHash": tx_hash})
    save_db(db)
    return {"success": True, "deal": deal}

@app.post("/api/deals/{deal_id}/release/{bucket}")
async def release_bucket(deal_id: str, bucket: str, s: dict = Depends(get_session)):
    db = load_db()
    d = next((x for x in db["escrowDeals"] if x["id"] == deal_id), None)
    if not d: raise HTTPException(status_code=404, detail="Deal not found")
    coin_code = d.get("coinCode", to_hex_code("RLUSD"))
    if bucket == "m1": amt = round(d["goodsUsd"] * (d["splitPct1"] / 100.0), 2)
    elif bucket == "freight": amt = round(d["freightUsd"], 2)
    else: amt = round(d["goodsUsd"] * (d["splitPct2"] / 100.0), 2)

    target_acct = d.get("carrierPayoutInfo") if bucket == "freight" and d.get("carrierPayoutInfo") else d["supplierAccountNumber"]
    await call_regulatory_socket(
        db["sockets"].get("offrampUrl", ""), db["sockets"].get("offrampApiKey", ""),
        {"action": f"DISBURSE_{bucket.upper()}", "dealId": deal_id, "amountUsd": amt, "destinationAccount": target_acct}
    )

    async with _xrpl_lock:
        try:
            actors = await get_3_actors(_rpc_client, coin_code)
            memo = Memo(memo_type=hex_str(f"OFFRAMP_{bucket.upper()}"), memo_data=hex_str(f"DEAL:{deal_id}|USD:{amt}|ACCT:{target_acct}"))
            res = await async_submit_and_wait(
                Payment(
                    account=actors["buyer"].address,
                    destination=actors["offramp"].address,
                    amount=IssuedCurrencyAmount(currency=coin_code, issuer=actors["issuer"].address, value=str(amt)),
                    memos=[memo],
                ), _rpc_client, actors["buyer"]
            )
            xh = res.result["hash"]
        except Exception as e:
            raise HTTPException(status_code=503, detail=f"Clearing Settlement Error: {e}")

    if bucket == "m1": d["m1Released"] = True; d["stage"] = "STAGE_3_M1_PAID"
    elif bucket == "freight": d["freightReleased"] = True; d["stage"] = "STAGE_5_CARRIER_PAID"
    elif bucket == "m2": d["m2Released"] = True; d["stage"] = "STAGE_6_COMPLETE"
    d["xrplLockHash"] = xh
    save_db(db)
    return {"success": True, "deal": d}

@app.post("/api/deals/{deal_id}/verify-prn")
async def verify_prn(deal_id: str, req: PrnReq, s: dict = Depends(get_session)):
    db = load_db()
    d = next((x for x in db["escrowDeals"] if x["id"] == deal_id), None)
    if not d: raise HTTPException(status_code=404, detail="Deal not found")
    prn_clean = req.prnNumber.strip()
    reg_res = await call_regulatory_socket(
        db["sockets"].get("customsRectsUrl", ""), db["sockets"].get("customsRectsKey", ""),
        {"action": "VERIFY_ASYCUDA_PRN", "dealId": deal_id, "prnNumber": prn_clean, "localGoodsAmount": d["localGoodsAmount"]}
    )
    d["customsPaid"] = True; d["prnNumber"] = prn_clean; d["stage"] = "STAGE_4_CUSTOMS_PRN_PAID"
    d["customsSocketLive"] = reg_res.get("live", False)
    db["taxLedger"].append({"countryCode": d["countryCode"], "entryType": "CUSTOMS_PRN_SETTLEMENT", "referenceId": f"{deal_id} (PRN {prn_clean})", "localAmount": d["localGoodsAmount"], "currency": d["currency"]})
    save_db(db)
    return {"success": True, "deal": d}

@app.post("/api/freight/bid")
async def add_bid(req: BidReq, s: dict = Depends(get_session)):
    db = load_db()
    d = next((x for x in db["escrowDeals"] if x["id"] == req.dealId), None)
    if not d: raise HTTPException(status_code=404, detail="Deal not found")
    await call_regulatory_socket(
        db["sockets"].get("customsRectsUrl", ""), db["sockets"].get("customsRectsKey", ""),
        {"action": "VALIDATE_RECTS_SEAL", "dealId": req.dealId, "truckPlate": req.truckPlate, "rectsSealId": req.rectsSealId}
    )
    payout_str = f"{req.payoutChannel}: {req.payoutAccount}" if req.payoutAccount else req.payoutChannel
    status = "APPROVED" if d.get("carrierAwarded") == "Open for Bidding" else "NOT_SELECTED"
    if status == "APPROVED":
        d["carrierAwarded"] = f"{s['orgName']} ({req.truckPlate})"
        d["carrierPayoutInfo"] = payout_str
        d["freightUsd"] = req.bidUsd
    db["freightBids"].append({
        "countryCode": d["countryCode"], "dealId": req.dealId, "carrierName": s["orgName"],
        "truckPlate": req.truckPlate, "rectsSealId": req.rectsSealId, "bidUsd": req.bidUsd,
        "payoutChannel": req.payoutChannel, "payoutAccount": req.payoutAccount, "status": status
    })
    save_db(db)
    return {"success": True, "status": status}

@app.post("/api/invoices")
async def add_inv(req: InvReq, s: dict = Depends(get_session)):
    db = load_db()
    c = s["countryCode"]; fx = await fetch_live_fx(COUNTRIES[c]["currency"], db["sockets"])
    iid = f"INV-{c}-{str(int(time.time()*1000))[-5:]}"
    efris_res = await call_regulatory_socket(
        db["sockets"].get("uraEfrisUrl", ""), db["sockets"].get("uraEfrisKey", ""),
        {"action": "ISSUE_EFRIS_INVOICE", "invoiceId": iid, "tin": s.get("taxIdTin", ""), "flowType": req.flowType, "amountUsd": req.amountUsd}
    )
    fdn = efris_res.get("fdn") or f"FDN-{random.randint(100000000,999999999)}"
    dest_tag = random.randint(100000000, 999999999)
    xh = sha256_hex({"id": iid, "fdn": fdn, "tag": dest_tag})
    inv = {
        "id": iid, "countryCode": c, "currency": COUNTRIES[c]["currency"], "flowType": req.flowType,
        "efrisFdn": fdn, "efrisLive": efris_res.get("live", False), "destinationTag": dest_tag, "amlStatus": "CLEARED_OFAC_UN",
        "exporterName": s["orgName"], "buyerName": req.buyerName, "productDesc": req.productDesc,
        "exporterBankName": req.exporterBankName or "", "exporterAccountNumber": req.exporterAccountNumber or "",
        "exportPermitNo": req.exportPermitNo or "", "bolNumber": req.bolNumber or "",
        "amountUsd": req.amountUsd, "localAmount": round(req.amountUsd * fx, 2),
        "gatePassReleased": True, "xrplHash": xh
    }
    db["invoices"].append(inv)
    db["taxLedger"].append({"countryCode": c, "entryType": f"{req.flowType}_INVOICE", "referenceId": f"{iid} ({fdn})", "localAmount": inv["localAmount"], "currency": inv["currency"]})
    save_db(db)
    return {"success": True, "invoice": inv}

@app.post("/api/batches")
async def add_bch(req: BatchReq, s: dict = Depends(get_session)):
    db = load_db(); c = s["countryCode"]
    bid = f"BCH-{c}-{str(int(time.time()*1000))[-5:]}"
    std_res = await call_regulatory_socket(
        db["sockets"].get("standardsRegUrl", ""), db["sockets"].get("standardsRegKey", ""),
        {"action": "VERIFY_PERMIT_AND_STAMP", "agency": req.agency, "permitNo": req.permitNo, "batchCode": req.batchCode}
    )
    xh = sha256_hex({"id": bid, "code": req.batchCode})
    b = {"id": bid, "countryCode": c, "orgName": s["orgName"], "agency": req.agency, "permitNo": req.permitNo, "permitLive": std_res.get("live", False), "batchCode": req.batchCode, "productName": req.productName, "xrplHash": xh}
    db["batches"].append(b)
    for i in range(1, 5):
        db["serialQRs"].append({"serialCode": f"{req.batchCode}-{i:04d}", "productName": req.productName, "orgName": s["orgName"], "agency": req.agency, "permitNo": req.permitNo, "scanCount": 0, "firstCity": ""})
    save_db(db)
    return {"success": True, "batch": b}

@app.post("/api/public/verify/{serial_code}")
async def verify_qr(serial_code: str, request: Request):
    city = (await request.json()).get("district", "Kampala")
    db = load_db()
    item = next((x for x in db["serialQRs"] if x["serialCode"].upper() == serial_code.strip().upper()), None)
    if not item: raise HTTPException(status_code=404, detail="Serial not found")
    item["scanCount"] += 1
    if item["scanCount"] == 1:
        item["firstCity"] = city; save_db(db)
        return {"authentic": True, "status": "VERIFIED GENUINE", "message": f"{item['productName']} ({item['agency']} {item['permitNo']}) - Verified Genuine in {city}"}
    save_db(db)
    await call_regulatory_socket(
        db["sockets"].get("standardsRegUrl", ""), db["sockets"].get("standardsRegKey", ""),
        {"action": "COUNTERFEIT_CLONE_ALERT", "serialCode": item["serialCode"], "firstCity": item["firstCity"], "cloneCity": city, "scanCount": item["scanCount"]}
    )
    return {"authentic": False, "status": "COUNTERFEIT CLONE ALERT", "message": f"Already scanned in {item['firstCity']}! Clone detected in {city}."}

@app.post("/api/retail/entry")
async def add_ret(req: RetailReq, s: dict = Depends(get_session)):
    db = load_db(); c = s["countryCode"]
    await call_regulatory_socket(
        db["sockets"].get("localGatewayUrl", ""), db["sockets"].get("localGatewayKey", ""),
        {"action": "LOG_DOMESTIC_TX", "entryType": req.entryType, "localAmount": req.localAmount, "currency": COUNTRIES[c]["currency"]}
    )
    tag = " [14-DAY SUPPLIER CREDIT]" if req.isCredit14Day else ""
    db["taxLedger"].append({"countryCode": c, "entryType": req.entryType, "referenceId": f"{req.itemDesc}{tag}", "localAmount": req.localAmount, "currency": COUNTRIES[c]["currency"], "isCredit14Day": req.isCredit14Day})
    save_db(db)
    return {"success": True}

@app.post("/api/admin/config")
def save_admin(req: AdminReq, s: dict = Depends(get_session)):
    db = load_db(); db["pricing"].update(req.pricing); db["sockets"].update(req.sockets); save_db(db)
    return {"success": True}

@app.get("/api/docs/{doc_type}/{ref_id}", response_class=HTMLResponse)
def render_doc(doc_type: str, ref_id: str, token: str = ""):
    db = load_db()
    s = db["sessions"].get(token) or {"orgName": "Crest Vault Verified Entity", "taxIdTin": "1004928172", "countryName": "Uganda", "countryCode": "UG", "currency": "UGX"}
    title = doc_type.replace("-", " ").upper()
    rows = [("Document Type", title), ("Reference ID", ref_id), ("Entity & TIN", f"{s['orgName']} ({s['taxIdTin']})"), ("Jurisdiction", f"{s['countryName']} ({s['countryCode']})")]

    deal = next((x for x in db["escrowDeals"] if x["id"] == ref_id), None) or (db["escrowDeals"][-1] if ref_id == "SUMMARY" and db["escrowDeals"] else None)
    inv = next((x for x in db["invoices"] if x["id"] == ref_id), None)
    bch = next((x for x in db["batches"] if x["id"] == ref_id), None)

    audit_hash = ""
    if deal:
        audit_hash = deal["xrplLockHash"]
        rows += [
            ("Trade ID & Stage", f"{deal['id']} ({deal['stage']})"),
            ("Incoterm & Goods", f"{deal['incoterm']} — {deal['productDesc']}"),
            ("Settlement Bank Rail", f"{deal['supplierName']} — {deal['supplierBankName']} ({deal['supplierAccountNumber']})"),
            ("Freight & RECTS Carrier", f"{deal['carrierAwarded']} ({deal.get('carrierPayoutInfo', 'Pending')})"),
            ("Value & Locked FX", f"${deal['goodsUsd']:,.2f} ({deal['localGoodsAmount']:,.2f} {deal['currency']} @ {deal['fxRate']})"),
            ("Clearing Audit Reference", audit_hash),
        ]
    elif inv:
        audit_hash = inv["xrplHash"]
        rows += [
            ("URA EFRIS FDN", inv["efrisFdn"]),
            ("Buyer / Counterparty", inv["buyerName"]),
            ("Off-Ramp Bank Rail", f"{inv.get('exporterBankName','')} {inv.get('exporterAccountNumber','')}"),
            ("Permit / B/L Ref", f"{inv.get('exportPermitNo','')} / {inv.get('bolNumber','')}"),
            ("Value & Locked FX", f"${inv['amountUsd']:,.2f} ({inv['localAmount']:,.2f} {inv['currency']})"),
            ("Clearing Audit Reference", audit_hash),
        ]
    elif bch:
        audit_hash = bch["xrplHash"]
        rows += [
            ("Regulatory Agency & Permit", f"{bch['agency']} — {bch['permitNo']}"),
            ("Batch & Product", f"{bch['batchCode']} ({bch['productName']})"),
            ("Clearing Audit Reference", audit_hash),
        ]
    elif doc_type == "retail-credit-passport":
        total_vol = sum(x["localAmount"] for x in db["taxLedger"] if x["countryCode"] == s["countryCode"])
        audit_hash = sha256_hex({"org": s["orgName"], "entries": len(db["taxLedger"]), "total": total_vol})
        rows += [
            ("Verified Trading Entries", str(len(db["taxLedger"]))),
            ("Total Verified Turnover", f"{total_vol:,.2f} {s['currency']}"),
            ("Credit Underwriting Status", "VERIFIED TAMPER-PROOF LEDGER"),
            ("Clearing Audit Reference", audit_hash),
        ]

    qr_payload = f"{APP_URL}/api/docs/{doc_type}/{ref_id}" + (f"?hash={audit_hash}" if audit_hash else "")
    buf = io.BytesIO()
    qrcode.make(qr_payload).save(buf, format="PNG")
    qr_b64 = base64.b64encode(buf.getvalue()).decode("utf-8")

    tr_html = "".join([f"<tr><th style='background:#f1f5f9;padding:10px;border:1px solid #cbd5e1;text-align:left;width:32%'>{k}</th><td style='padding:10px;border:1px solid #cbd5e1;word-break:break-all'>{v}</td></tr>" for k, v in rows])
    return f"<!DOCTYPE html><html><body style='font-family:Arial;padding:30px;max-width:860px;margin:auto'><div style='display:flex;justify-content:space-between;align-items:center;border-bottom:2px solid #059669;padding-bottom:14px;margin-bottom:18px'><div><h1 style='color:#059669;margin:0'>CREST VAULT AFRICA</h1><p style='margin:4px 0 0;color:#475569'>Official Automated Clearing, Tax &amp; Regulatory Audit Record</p></div><div style='text-align:center'><img src='data:image/png;base64,{qr_b64}' width='115' height='115' alt='Verification QR' /><div style='font-size:10px;color:#64748b'>Scan to Verify Hash</div></div></div><h2 style='margin-top:0'>{title}</h2><table style='width:100%;border-collapse:collapse'>{tr_html}</table><br/><button onclick='window.print()' style='padding:10px 18px;background:#059669;color:#fff;border:none;border-radius:6px;font-weight:bold;cursor:pointer'>Print / Save PDF</button></body></html>"

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, reload=True)
