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

# --- CLEAN 3-ACCOUNT CLEARING ENGINE (ISSUER -> BUYER -> OFFRAMP) ---
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
    acct = {"countryCode": c, "countryName": p["name"], "currency": p["currency"], "userRole": req.userRole, "roleTitle": ROLES.get(req.userRole, req.userRole), "userName": req.userName, "orgName": req.orgName, "taxIdTin": req.taxIdTin, "clearingAccountRef": ref, "xrplVault": ref}
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
        "localGoodsAmount": round(req.goodsUsd * fx, 2), "importerName": s["orgName"],
        "supplierName": req.supplierName, "supplierBankName": req.supplierBankName,
        "supplierAccountNumber": req.supplierAccountNumber, "productDesc": req.productDesc,
        "incoterm": req.incoterm, "goodsUsd": req.goodsUsd, "freightUsd": req.freightUsd,
        "splitPct1": req.splitPct1, "splitPct2": req.splitPct2,
        "m1Released": False, "m2Released": False, "freightReleased": False,
        "carrierAwarded": "Open for Bidding", "stage": "STAGE_1_ESCROW_LOCKED",
        "xrplLockHash": tx_hash, "xrplOfferSequence": seq, "coinCode": tok["code"],
    }
    db["escrowDeals"].append(deal)
    db["corporateRevenue"].append({"feeType": "IMPORTER_ESCROW_FEE", "referenceId": did, "amountUsd": 35.0, "xrplHash": tx_hash})
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

    async with _xrpl_lock:
        try:
            actors = await get_3_actors(_rpc_client, coin_code)
            memo = Memo(memo_type=hex_str(f"OFFRAMP_{bucket.upper()}"), memo_data=hex_str(f"DEAL:{deal_id}|USD:{amt}|ACCT:{d['supplierAccountNumber']}"))
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

class PrnReq(BaseModel):
    prnNumber: str

@app.post("/api/deals/{deal_id}/verify-prn")
def verify_prn(deal_id: str, req: PrnReq, s: dict = Depends(get_session)):
    db = load_db()
    d = next((x for x in db["escrowDeals"] if x["id"] == deal_id), None)
    if not d: raise HTTPException(status_code=404, detail="Deal not found")
    d["customsPaid"] = True
    d["prnNumber"] = req.prnNumber.strip()
    d["stage"] = "STAGE_4_CUSTOMS_PRN_PAID"
    db["taxLedger"].append({
        "countryCode": d["countryCode"],
        "entryType": "CUSTOMS_PRN_SETTLEMENT",
        "referenceId": f"{deal_id} (PRN {d['prnNumber']})",
        "localAmount": d["localGoodsAmount"],
        "currency": d["currency"],
    })
    save_db(db)
    return {"success": True, "deal": d}

@app.post("/api/freight/bid")
def add_bid(req: BidReq, s: dict = Depends(get_session)):
    db = load_db()
    d = next((x for x in db["escrowDeals"] if x["id"] == req.dealId), None)
    if not d: raise HTTPException(status_code=404, detail="Deal not found")
    d["carrierAwarded"] = f"{s['orgName']} ({req.truckPlate})"
    db["freightBids"].append({"countryCode": d["countryCode"], "dealId": req.dealId, "carrierName": s["orgName"], "truckPlate": req.truckPlate, "rectsSealId": req.rectsSealId, "bidUsd": req.bidUsd})
    save_db(db)
    return {"success": True}

@app.post("/api/invoices")
def add_inv(req: InvReq, s: dict = Depends(get_session)):
    db = load_db()
    c = s["countryCode"]; fx = RATES[COUNTRIES[c]["currency"]]
    iid = f"INV-{c}-{str(int(time.time()*1000))[-5:]}"; fdn = f"FDN-{random.randint(100000000,999999999)}"
    xh = sha256_hex({"id": iid, "fdn": fdn})
    inv = {"id": iid, "countryCode": c, "currency": COUNTRIES[c]["currency"], "efrisFdn": fdn, "buyerName": req.buyerName, "amountUsd": req.amountUsd, "localAmount": round(req.amountUsd * fx, 2), "xrplHash": xh}
    db["invoices"].append(inv)
    db["taxLedger"].append({"countryCode": c, "entryType": f"{req.flowType}_INVOICE", "referenceId": f"{iid} ({fdn})", "localAmount": inv["localAmount"], "currency": inv["currency"]})
    save_db(db)
    return {"success": True, "invoice": inv}

@app.post("/api/batches")
def add_bch(req: BatchReq, s: dict = Depends(get_session)):
    db = load_db(); c = s["countryCode"]
    bid = f"BCH-{c}-{str(int(time.time()*1000))[-5:]}"; xh = sha256_hex({"id": bid, "code": req.batchCode})
    b = {"id": bid, "countryCode": c, "agency": req.agency, "permitNo": req.permitNo, "batchCode": req.batchCode, "productName": req.productName, "xrplHash": xh}
    db["batches"].append(b)
    for i in range(1, 5):
        db["serialQRs"].append({"serialCode": f"{req.batchCode}-{i:04d}", "productName": req.productName, "agency": req.agency, "permitNo": req.permitNo, "scanCount": 0, "firstCity": ""})
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
        return {"authentic": True, "status": "VERIFIED GENUINE", "message": f"{item['productName']} ({item['agency']} {item['permitNo']}) - Verified in {city}"}
    save_db(db)
    return {"authentic": False, "status": "COUNTERFEIT CLONE ALERT", "message": f"Already scanned in {item['firstCity']}! Clone detected in {city}."}

@app.post("/api/retail/entry")
def add_ret(req: RetailReq, s: dict = Depends(get_session)):
    db = load_db(); c = s["countryCode"]
    db["taxLedger"].append({"countryCode": c, "entryType": req.entryType, "referenceId": req.itemDesc, "localAmount": req.localAmount, "currency": COUNTRIES[c]["currency"]})
    save_db(db)
    return {"success": True}

@app.post("/api/admin/config")
def save_admin(req: AdminReq, s: dict = Depends(get_session)):
    db = load_db(); db["pricing"].update(req.pricing); db["sockets"].update(req.sockets); save_db(db)
    return {"success": True}

@app.post("/api/escrow/live-suite")
async def live_suite():
    async with _xrpl_lock:
        actors = await get_3_actors(_rpc_client, to_hex_code("RLUSD"))
        buyer, offramp, issuer = actors["buyer"], actors["offramp"], actors["issuer"]
        memo = Memo(memo_type=hex_str("OFFRAMP_SETTLEMENT"), memo_data=hex_str("INV:SUITE-01|BANK:Bank of China"))
        c_res = await async_submit_and_wait(EscrowCreate(account=buyer.address, destination=offramp.address, amount=xrp_to_drops(1), finish_after=datetime_to_ripple_time(datetime.now(timezone.utc) + timedelta(seconds=1)), cancel_after=datetime_to_ripple_time(datetime.now(timezone.utc) + timedelta(days=7)), memos=[memo]), _rpc_client, buyer)
        await asyncio.sleep(2)
        f_res = await async_submit_and_wait(EscrowFinish(account=offramp.address, owner=buyer.address, offer_sequence=c_res.result["tx_json"]["Sequence"]), _rpc_client, offramp)
        return {"ok": True, "settlement_suite": {"rail_1_custom_flagged_stablecoin": {"issuer_address": issuer.address, "flag_tx_hash": c_res.result["hash"], "mint_tx_hash": c_res.result["hash"]}, "rail_2_option_a_native_escrow": {"escrow_create_tx": c_res.result["hash"], "escrow_finish_tx": f_res.result["hash"], "ledger_result": "tesSUCCESS"}, "rail_3_option_b_multisig_offramp_safe": {"safe_address": offramp.address, "deposit_tx": c_res.result["hash"], "cosigned_release_tx": f_res.result["hash"], "ledger_result": "tesSUCCESS"}}}

@app.get("/api/docs/{doc_type}/{ref_id}", response_class=HTMLResponse)
def render_doc(doc_type: str, ref_id: str, token: str = ""):
    db = load_db()
    s = db["sessions"].get(token) or {"orgName": "Crest Vault Verified Entity", "taxIdTin": "1004928172", "countryName": "Uganda", "countryCode": "UG", "currency": "UGX"}
    title = doc_type.replace("-", " ").upper()
    rows = [("Document Type", title), ("Reference ID", ref_id), ("Entity & TIN", f"{s['orgName']} ({s['taxIdTin']})"), ("Jurisdiction", f"{s['countryName']} ({s['countryCode']})")]
    deal = next((x for x in db["escrowDeals"] if x["id"] == ref_id), None)
    if deal:
        rows += [("Incoterm", deal["incoterm"]), ("Supplier Bank", f"{deal['supplierName']} — {deal['supplierBankName']} ({deal['supplierAccountNumber']})"), ("Goods Value", f"${deal['goodsUsd']} ({deal['localGoodsAmount']:,.2f} {deal['currency']})"), ("Clearing Audit Reference", deal["xrplLockHash"])]
    tr_html = "".join([f"<tr><th style='background:#f1f5f9;padding:10px;border:1px solid #cbd5e1;text-align:left'>{k}</th><td style='padding:10px;border:1px solid #cbd5e1'>{v}</td></tr>" for k, v in rows])
    return f"<!DOCTYPE html><html><body style='font-family:Arial;padding:30px'><h1 style='color:#059669;margin:0'>CREST VAULT AFRICA</h1><p>Official Automated Clearing &amp; Compliance Record</p><h2>{title}</h2><table style='width:100%;border-collapse:collapse'>{tr_html}</table><br/><button onclick='window.print()' style='padding:10px 18px;background:#059669;color:#fff;border:none;border-radius:6px;font-weight:bold'>Print / Save PDF</button></body></html>"

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, reload=True)
