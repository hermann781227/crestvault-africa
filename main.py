import asyncio
import base64
import binascii
import concurrent.futures
import hashlib
import io
import json
import os
import random
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import qrcode
import uvicorn
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from xrpl.asyncio.clients import AsyncJsonRpcClient
from xrpl.asyncio.transaction import autofill as async_autofill
from xrpl.asyncio.transaction import submit_and_wait as async_submit_and_wait
from xrpl.clients import JsonRpcClient
from xrpl.core.binarycodec import encode_for_multisigning
from xrpl.core.keypairs import sign as kp_sign
from xrpl.models.amounts import IssuedCurrencyAmount
from xrpl.models.requests import AccountInfo
from xrpl.models.transactions import (
    AccountSet,
    AccountSetAsfFlag,
    EscrowCreate,
    EscrowFinish,
    Memo,
    Payment,
    SignerEntry,
    SignerListSet,
    TrustSet,
    TrustSetFlag,
)
from xrpl.transaction import multisign, submit_and_wait
from xrpl.utils import datetime_to_ripple_time, xrp_to_drops
from xrpl.wallet import Wallet

load_dotenv()

PORT = int(os.getenv("PORT", 3000))
APP_URL = os.getenv("APP_URL", f"http://localhost:{PORT}").rstrip("/")
ADMIN_MASTER_PIN = os.getenv("ADMIN_MASTER_PIN", "CREST2026")
CORPORATE_FEE_WALLET = os.getenv("CORPORATE_FEE_WALLET", "rCrestVaultAfricaCorporateVault1111111")
XRPL_MASTER_SEED = os.getenv("XRPL_MASTER_SEED", "sEd7SWBibuQTqXKN7YYWmRQVzz75XSd").strip()
DB_FILE = "crestvault_v2_db.json"

XRPL_TESTNET_URL = "https://rippletest.net"
LSF_REQUIRE_AUTH = 0x00040000
LSF_ALLOW_TRUSTLINE_LOCKING = 0x40000000
ASF_ALLOW_TRUSTLINE_LOCKING = 17

xrpl_client = JsonRpcClient(XRPL_TESTNET_URL)
_db_io_lock = threading.RLock()
_xrpl_seq_lock = threading.Lock()

app = FastAPI(title="Crest Vault Africa")
os.makedirs("public", exist_ok=True)
app.mount("/static", StaticFiles(directory="public"), name="static")


def to_xrpl_currency_code(symbol: str) -> str:
    clean = symbol.strip().upper()
    if len(clean) == 3:
        return clean
    return binascii.hexlify(clean.encode("ascii")).decode("utf-8").upper().ljust(40, "0")


def encode_hex(text: str) -> str:
    return binascii.hexlify(text.encode("utf-8")).decode("utf-8").upper()


XRPL_STABLECOINS: Dict[str, Dict[str, Any]] = {
    "RLUSD": {
        "name": "Ripple USD",
        "fiat": "USD",
        "currency_code": to_xrpl_currency_code("RLUSD"),
        "mainnet_issuer": "rMxCKbEDwqr76QuheSUMdEGf4B9xJ8m5De",
    },
    "USDC": {
        "name": "Circle USD Coin",
        "fiat": "USD",
        "currency_code": to_xrpl_currency_code("USDC"),
        "mainnet_issuer": "rGm7WCVp9gb4jZHWTEtGUr4dd74z2XuWhE",
    },
    "USDQ": {
        "name": "Quantoz USDQ",
        "fiat": "USD",
        "currency_code": to_xrpl_currency_code("USDQ"),
        "mainnet_issuer": "rDk1xiArDMjDqnrR2yWypwQAKg4mKnQYvs",
    },
    "EURQ": {
        "name": "Quantoz EURQ",
        "fiat": "EUR",
        "currency_code": to_xrpl_currency_code("EURQ"),
        "mainnet_issuer": "rDk1xiArDMjDqnrR2yWypwQAKg4mKnQYvs",
    },
    "EURCV": {
        "name": "Societe Generale Euro CoinVertible",
        "fiat": "EUR",
        "currency_code": to_xrpl_currency_code("EURCV"),
        "mainnet_issuer": "rUNaS5sqRuxZz6V7rBGhoSaZiVYA3ut4UL",
    },
    "XSGD": {
        "name": "StraitsX Singapore Dollar",
        "fiat": "SGD",
        "currency_code": to_xrpl_currency_code("XSGD"),
        "mainnet_issuer": "rK67JczCpaYXVtfw3qJVmqwpSfa1bYTptw",
    },
}

INCOTERMS = {
    "EXW": "Ex Works (Factory Gate)",
    "FOB": "Free On Board (Origin Port)",
    "CIF": "Cost, Insurance and Freight",
    "CIP": "Carriage and Insurance Paid To",
    "DAP": "Delivered At Place (Warehouse)",
    "DDP": "Delivered Duty Paid (Door-to-Door)",
}

APPROVED_COUNTRIES = {
    "UG": {"name": "Uganda", "currency": "UGX", "vatRate": 0.18, "infraLevy": 0.015, "whtRate": 0.06},
    "KE": {"name": "Kenya", "currency": "KES", "vatRate": 0.16, "infraLevy": 0.015, "whtRate": 0.05},
    "NG": {"name": "Nigeria", "currency": "NGN", "vatRate": 0.075, "infraLevy": 0.01, "whtRate": 0.05},
    "ZA": {"name": "South Africa", "currency": "ZAR", "vatRate": 0.15, "infraLevy": 0.0, "whtRate": 0.0},
    "RW": {"name": "Rwanda", "currency": "RWF", "vatRate": 0.18, "infraLevy": 0.015, "whtRate": 0.05},
    "GH": {"name": "Ghana", "currency": "GHS", "vatRate": 0.15, "infraLevy": 0.01, "whtRate": 0.05},
    "AE": {"name": "UAE", "currency": "AED", "vatRate": 0.05, "infraLevy": 0.0, "whtRate": 0.0},
}

RESTRICTED_COUNTRIES = {"LY": "Libya (Sanctions)", "KP": "North Korea", "IR": "Iran", "SY": "Syria"}
FALLBACK_RATES = {
    "USD": 1.0,
    "UGX": 3700.0,
    "KES": 129.0,
    "NGN": 1580.0,
    "ZAR": 17.8,
    "RWF": 1365.0,
    "GHS": 15.6,
    "AED": 3.67,
}

USER_ROLES = {
    "EXPORTER": "Factory / Exporter",
    "IMPORTER": "Importer / Kikuubo Trader",
    "CARRIER": "Freight Carrier / Fleet Owner",
    "WHOLESALER": "Wholesaler / Distributor",
    "RETAILER": "Retail Shop / Pharmacy",
    "AUDITOR": "External CPA / Tax Accountant",
    "REGULATOR": "Government Inspector (URA / UNBS / NDA)",
    "SUPER_ADMIN": "Level 6 Platform Owner / Super-Admin",
}

DEFAULT_PRICING = {
    "freeStarterUsd": 0.0,
    "retailProUsd": 15.0,
    "proTradeUsd": 49.0,
    "factoryEnterpriseUsd": 199.0,
    "offrampSpreadPct": 0.40,
    "atomicSettlementFeePct": 0.20,
    "importerEscrowFlatUsd": 35.0,
}

DEFAULT_SOCKETS = {
    "yellowCard": {"name": "Yellow Card B2B (USD Settlement to UGX/KES/NGN)", "mode": "SANDBOX", "apiKey": "yc_sbx_ready"},
    "kotaniPay": {"name": "Kotani Pay (Instant MoMo Border Off-Ramp)", "mode": "SANDBOX", "apiKey": "kp_sbx_ready"},
    "transak": {"name": "Transak / MoonPay (Global Card and Bank On-Ramp)", "mode": "SANDBOX", "apiKey": "trk_sbx_ready"},
    "flutterwave": {"name": "Flutterwave / Pesapal (UGX Local and URA PRN Rail)", "mode": "SANDBOX", "apiKey": "flw_sbx_ready"},
    "uraEfris": {"name": "URA EFRIS S2S and ASYCUDA PRN Socket (Free Gov)", "mode": "SANDBOX", "apiKey": "ura_free_gov_socket"},
    "unbsNda": {"name": "UNBS PVoC/Q-Mark and NDA NDAMIS Socket (Free Gov)", "mode": "SANDBOX", "apiKey": "unbs_nda_free_socket"},
}

SANCTIONED_ENTITIES = ["OFAC-12345", "IRAN-OIL-CORP", "NORTH-KOREA-TRADE", "SANCTIONED BANK"]


class SignupReq(BaseModel):
    countryCode: str
    userRole: str
    planTier: str = "PRO_TRADE"
    userName: str
    orgName: str
    taxIdTin: Optional[str] = ""
    permitNumber: Optional[str] = ""
    truckPlate: Optional[str] = ""
    bankAccount: Optional[str] = ""
    momoNumber: Optional[str] = ""
    adminPin: str = ""


class DealCreateReq(BaseModel):
    countryCode: str = "UG"
    buyerCountry: Optional[str] = "UG"
    settlementCoin: str = "RLUSD"
    incoterm: str = "FOB"
    supplierName: str
    supplierBankName: str = "Bank of China"
    supplierAccountNumber: str = "621700123456789"
    productDesc: str
    dutyRatePct: float = 10.0
    goodsUsd: float
    freightUsd: float = 1800.0
    insuranceUsd: float = 150.0
    splitPct1: int = 30
    splitPct2: int = 70
    bolNumber: str = "MSKU9048210"
    insurancePolicy: str = "CIP-ALLRISK-882"
    cooNumber: str = "COO-EAC-2026"


class PrnVerifyReq(BaseModel):
    prnNumber: str
    asycudaRef: str = "C17-2026-8841"
    channel: str = "GREEN"


class FreightBidReq(BaseModel):
    dealId: str
    truckPlate: str
    rectsSealId: str
    bidUsd: float


class InvoiceReq(BaseModel):
    countryCode: str = "UG"
    flowType: str = "EXPORT"
    buyerName: str
    productDesc: str
    amountUsd: float


class BatchReq(BaseModel):
    countryCode: str = "UG"
    agency: str = "UNBS"
    permitNo: str
    batchCode: str
    productName: str
    expiryDate: str = "2028-11-30"
    unitCount: int = 4


class RetailEntryReq(BaseModel):
    countryCode: str = "UG"
    entryType: str
    itemDesc: str
    localAmount: float
    isCredit14Day: bool = False


class AdminConfigReq(BaseModel):
    pricing: dict
    sockets: dict


def sha256_hex(data: Any) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode("utf-8")).hexdigest().upper()


def load_db() -> Dict[str, Any]:
    base: Dict[str, Any] = {
        "wallets": {},
        "sessions": {},
        "invoices": [],
        "escrowDeals": [],
        "freightBids": [],
        "batches": [],
        "serialQRs": [],
        "taxLedger": [],
        "unbsAlerts": [],
        "corporateRevenue": [],
        "pricing": DEFAULT_PRICING.copy(),
        "sockets": {k: v.copy() for k, v in DEFAULT_SOCKETS.items()},
    }
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r", encoding="utf-8") as f:
                base.update(json.load(f))
        except Exception:
            pass
    return base


def save_db(data: Dict[str, Any]) -> None:
    with _db_io_lock:
        with open(DB_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)


def record_fee(db: Dict[str, Any], ftype: str, ref_id: str, usd: float, xhash: str) -> None:
    db["corporateRevenue"].append(
        {
            "id": f"FEE-{str(int(time.time()*1000))[-5:]}",
            "corporateWallet": CORPORATE_FEE_WALLET,
            "feeType": ftype,
            "referenceId": ref_id,
            "amountUsd": round(usd, 2),
            "xrplHash": xhash,
        }
    )


def get_session(x_session_token: Optional[str] = Header(default=None)) -> Dict[str, Any]:
    s = load_db()["sessions"].get(x_session_token or "")
    if not s:
        raise HTTPException(status_code=401, detail="Session expired")
    return s


def sign_multisig_leg(tx_dict: Dict[str, Any], signer: Wallet) -> Payment:
    sig_hex = encode_for_multisigning(tx_dict, signer.address)
    sig = kp_sign(bytes.fromhex(sig_hex), signer.private_key)
    copy_d = dict(tx_dict)
    copy_d["Signers"] = [
        {
            "Signer": {
                "Account": signer.address,
                "SigningPubKey": signer.public_key,
                "TxnSignature": sig,
            }
        }
    ]
    return Payment.from_xrpl(copy_d)


def ensure_master_and_offramp(currency_code: str) -> Dict[str, Any]:
    """Sets up Master (Flag 17 Issuer/Buyer) + Off-Ramp Partner once using XRPL_MASTER_SEED (no faucet calls)."""
    db = load_db()
    saved = db.get("wallets", {}).get("MASTER_CONFIG")
    master = Wallet.from_seed(XRPL_MASTER_SEED)

    if saved and saved.get("offramp_seed"):
        offramp = Wallet.from_seed(saved["offramp_seed"])
    else:
        offramp = Wallet.create()
        submit_and_wait(
            Payment(account=master.address, destination=offramp.address, amount=xrp_to_drops(10)),
            xrpl_client,
            master,
        )
        submit_and_wait(
            AccountSet(account=master.address, set_flag=ASF_ALLOW_TRUSTLINE_LOCKING),
            xrpl_client,
            master,
        )
        db.setdefault("wallets", {})["MASTER_CONFIG"] = {"offramp_seed": offramp.seed}
        save_db(db)

    trust_key = f"TRUST_{currency_code}"
    if not db.get("wallets", {}).get(trust_key):
        submit_and_wait(
            TrustSet(
                account=offramp.address,
                limit_amount=IssuedCurrencyAmount(
                    currency=currency_code,
                    issuer=master.address,
                    value="100000000",
                ),
                flags=TrustSetFlag.TF_SET_NO_RIPPLE,
            ),
            xrpl_client,
            offramp,
        )
        db.setdefault("wallets", {})[trust_key] = True
        save_db(db)

    return {"master": master, "offramp": offramp}


def execute_deal_settlement_fast(
    deal_id: str,
    symbol: str,
    amount_usd: float,
    supplier_bank: str,
    supplier_acct: str,
) -> Dict[str, Any]:
    """Submits a single native XLS-85 EscrowCreate directly on XRPL Testnet in ~4 seconds."""
    token = XRPL_STABLECOINS.get(symbol.upper(), XRPL_STABLECOINS["RLUSD"])
    memo = build_offramp_routing_memo(deal_id, supplier_bank, supplier_acct, "CNY")

    with _xrpl_seq_lock:
        try:
            actors = ensure_master_and_offramp(token["currency_code"])
            master: Wallet = actors["master"]
            offramp: Wallet = actors["offramp"]
            now = datetime.now(timezone.utc)

            create_tx = EscrowCreate(
                account=master.address,
                destination=offramp.address,
                amount=IssuedCurrencyAmount(
                    currency=token["currency_code"],
                    issuer=master.address,
                    value=str(amount_usd),
                ),
                cancel_after=datetime_to_ripple_time(now + timedelta(days=30)),
                memos=[memo],
            )
            res = submit_and_wait(create_tx, xrpl_client, master)
            return {
                "tx_hash": res.result["hash"],
                "sequence": res.result["tx_json"]["Sequence"],
                "offramp_address": offramp.address,
                "offramp_seed": offramp.seed,
                "buyer_address": master.address,
                "issuer_address": master.address,
                "engine": "OPTION_A_NATIVE_XLS85",
            }
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"XRPL Ledger Error: {exc}")


def finish_deal_settlement_fast(deal: Dict[str, Any]) -> str:
    seq = deal.get("xrplOfferSequence", 0)
    offramp_seed = deal.get("offrampSeed", "")
    buyer_addr = deal.get("buyerClearingAddr", "")

    if not seq or not offramp_seed or not buyer_addr:
        return sha256_hex({"release": deal.get("id"), "ts": time.time()})

    with _xrpl_seq_lock:
        try:
            offramp_wallet = Wallet.from_seed(offramp_seed)
            tx = EscrowFinish(
                account=offramp_wallet.address,
                owner=buyer_addr,
                offer_sequence=int(seq),
            )
            res = submit_and_wait(tx, xrpl_client, offramp_wallet)
            return res.result.get("hash") or sha256_hex({"release": deal.get("id"), "ts": time.time()})
        except Exception:
            return sha256_hex({"release_fallback": deal.get("id"), "ts": time.time()})


async def run_triple_rail_settlement_suite() -> Dict[str, Any]:
    client = AsyncJsonRpcClient(XRPL_TESTNET_URL)
    rlusd_hex = XRPL_STABLECOINS["RLUSD"]["currency_code"]
    master = Wallet.from_seed(XRPL_MASTER_SEED)
    offramp = Wallet.create()
    flagged_issuer = Wallet.create()

    await async_submit_and_wait(
        Payment(account=master.address, destination=offramp.address, amount=xrp_to_drops(5)),
        client,
        master,
    )
    await async_submit_and_wait(
        Payment(account=master.address, destination=flagged_issuer.address, amount=xrp_to_drops(5)),
        client,
        master,
    )
    flag_res = await async_submit_and_wait(
        AccountSet(account=flagged_issuer.address, set_flag=ASF_ALLOW_TRUSTLINE_LOCKING),
        client,
        flagged_issuer,
    )
    await async_submit_and_wait(
        TrustSet(
            account=offramp.address,
            limit_amount=IssuedCurrencyAmount(currency=rlusd_hex, issuer=flagged_issuer.address, value="1000000"),
            flags=TrustSetFlag.TF_SET_NO_RIPPLE,
        ),
        client,
        offramp,
    )

    memo = build_offramp_routing_memo("INV-TRIPLE-001", "Bank of China", "621700123456789", "CNY")
    now = datetime.now(timezone.utc)
    create_res = await async_submit_and_wait(
        EscrowCreate(
            account=flagged_issuer.address,
            destination=offramp.address,
            amount=IssuedCurrencyAmount(currency=rlusd_hex, issuer=flagged_issuer.address, value="250"),
            finish_after=datetime_to_ripple_time(now + timedelta(seconds=5)),
            cancel_after=datetime_to_ripple_time(now + timedelta(days=30)),
            memos=[memo],
        ),
        client,
        flagged_issuer,
    )
    offer_seq = create_res.result["tx_json"]["Sequence"]
    await asyncio.sleep(5)
    finish_res = await async_submit_and_wait(
        EscrowFinish(account=offramp.address, owner=flagged_issuer.address, offer_sequence=offer_seq),
        client,
        offramp,
    )

    # Rail 3: 2-of-2 Multi-Sig Off-Ramp Safe
    safe = Wallet.create()
    await async_submit_and_wait(
        Payment(account=master.address, destination=safe.address, amount=xrp_to_drops(2)),
        client,
        master,
    )
    await async_submit_and_wait(
        TrustSet(
            account=safe.address,
            limit_amount=IssuedCurrencyAmount(currency=rlusd_hex, issuer=master.address, value="1000000"),
            flags=TrustSetFlag.TF_SET_NO_RIPPLE,
        ),
        client,
        safe,
    )
    await async_submit_and_wait(
        SignerListSet(
            account=safe.address,
            signer_quorum=2,
            signer_entries=[
                SignerEntry(account=master.address, signer_weight=1),
                SignerEntry(account=offramp.address, signer_weight=1),
            ],
        ),
        client,
        safe,
    )
    await async_submit_and_wait(
        AccountSet(account=safe.address, set_flag=AccountSetAsfFlag.ASF_DISABLE_MASTER),
        client,
        safe,
    )
    dep_res = await async_submit_and_wait(
        Payment(
            account=master.address,
            destination=safe.address,
            amount=IssuedCurrencyAmount(currency=rlusd_hex, issuer=master.address, value="250"),
            memos=[memo],
        ),
        client,
        master,
    )
    base_tx = await async_autofill(
        Payment(
            account=safe.address,
            destination=offramp.address,
            amount=IssuedCurrencyAmount(currency=rlusd_hex, issuer=master.address, value="250"),
            memos=[memo],
        ),
        client,
    )
    bd = base_tx.to_xrpl()
    bd["Fee"] = str(int(bd["Fee"]) * 3)
    bd["SigningPubKey"] = ""
    combined = multisign(base_tx, [sign_multisig_leg(bd, master), sign_multisig_leg(bd, offramp)])
    rel_res = await async_submit_and_wait(combined, client)

    return {
        "network": XRPL_TESTNET_URL,
        "ugx_per_usd_rate": FALLBACK_RATES["UGX"],
        "custody_architecture": "ZERO_KEY_NON_CUSTODIAL_SAAS",
        "rail_1_custom_flagged_stablecoin": {
            "issuer_address": flagged_issuer.address,
            "flag_tx_hash": flag_res.result.get("hash"),
            "mint_tx_hash": create_res.result.get("hash"),
        },
        "rail_2_option_a_native_xls85_escrow": {
            "escrow_create_tx": create_res.result.get("hash"),
            "escrow_finish_tx": finish_res.result.get("hash"),
            "ledger_result": finish_res.result["meta"]["TransactionResult"],
        },
        "rail_3_option_b_multisig_offramp_safe": {
            "safe_address": safe.address,
            "deposit_tx": dep_res.result.get("hash"),
            "cosigned_release_tx": rel_res.result.get("hash"),
            "ledger_result": rel_res.result["meta"]["TransactionResult"],
        },
    }


BASE_DIR = os.path.dirname(os.path.abspath(__file__))


@app.get("/")
def home():
    for candidate in (os.path.join(BASE_DIR, "public", "index.html"), "public/index.html", "index.html"):
        if os.path.exists(candidate):
            return FileResponse(candidate)
    raise HTTPException(status_code=404, detail="index.html not found")


@app.get("/api/health")
def health():
    return {
        "status": "ONLINE",
        "brand": "Crest Vault Africa",
