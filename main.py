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

# XRPL IMPORTS
from xrpl.asyncio.clients import AsyncJsonRpcClient
from xrpl.asyncio.transaction import autofill as async_autofill
from xrpl.asyncio.transaction import multisign as async_multisign
from xrpl.asyncio.transaction import submit_and_wait as async_submit_and_wait
from xrpl.asyncio.wallet import generate_faucet_wallet
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
from xrpl.transaction import submit_and_wait
from xrpl.utils import datetime_to_ripple_time, xrp_to_drops
from xrpl.wallet import Wallet

load_dotenv()

PORT = int(os.getenv("PORT", 3000))
APP_URL = os.getenv("APP_URL", f"http://localhost:{PORT}").rstrip("/")
ADMIN_MASTER_PIN = os.getenv("ADMIN_MASTER_PIN", "CREST2026")
CORPORATE_FEE_WALLET = os.getenv("CORPORATE_FEE_WALLET", "rCrestVaultAfricaCorporateVault1111111")
OFFRAMP_HOLDING_ADDRESS = os.getenv("OFFRAMP_HOLDING_ADDRESS", "")
DB_FILE = "crestvault_v2_db.json"

# XRPL NETWORK & PROTOCOL CONSTANTS
XRPL_TESTNET_URL = os.getenv("XRPL_RPC_URL", "https://rippletest.net")
LSF_REQUIRE_AUTH = 0x00040000
LSF_ALLOW_TRUSTLINE_LOCKING = 0x40000000
ASF_ALLOW_TRUSTLINE_LOCKING = 17

xrpl_client = JsonRpcClient(XRPL_TESTNET_URL)
_db_io_lock = threading.RLock()

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


# --- MULTI-STABLECOIN ISSUER REGISTRY (XRPL-NATIVE) ---
XRPL_STABLECOINS: Dict[str, Dict[str, Any]] = {
    "RLUSD": {
        "name": "Ripple USD",
        "fiat": "USD",
        "currency_code": to_xrpl_currency_code("RLUSD"),
        "mainnet_issuer": "rMxCKbEDwqr76QuheSUMdEGf4B9xJ8m5De",
        "testnet_issuer": None,  # Uses live Flag-17 issuer provisioning on Testnet
    },
    "USDC": {
        "name": "Circle USD Coin",
        "fiat": "USD",
        "currency_code": to_xrpl_currency_code("USDC"),
        "mainnet_issuer": "rGm7WCVp9gb4jZHWTEtGUr4dd74z2XuWhE",
        "testnet_issuer": "rHuGNhqTG32mfmAvWA8hUyWRLV3tCSwKQt",
    },
    "USDQ": {
        "name": "Quantoz USDQ",
        "fiat": "USD",
        "currency_code": to_xrpl_currency_code("USDQ"),
        "mainnet_issuer": "rDk1xiArDMjDqnrR2yWypwQAKg4mKnQYvs",
        "testnet_issuer": None,
    },
    "EURQ": {
        "name": "Quantoz EURQ",
        "fiat": "EUR",
        "currency_code": to_xrpl_currency_code("EURQ"),
        "mainnet_issuer": "rDk1xiArDMjDqnrR2yWypwQAKg4mKnQYvs",
        "testnet_issuer": None,
    },
    "EURCV": {
        "name": "Societe Generale Euro CoinVertible",
        "fiat": "EUR",
        "currency_code": to_xrpl_currency_code("EURCV"),
        "mainnet_issuer": "rUNaS5sqRuxZz6V7rBGhoSaZiVYA3ut4UL",
        "testnet_issuer": None,
    },
    "XSGD": {
        "name": "StraitsX Singapore Dollar",
        "fiat": "SGD",
        "currency_code": to_xrpl_currency_code("XSGD"),
        "mainnet_issuer": "rK67JczCpaYXVtfw3qJVmqwpSfa1bYTptw",
        "testnet_issuer": None,
    },
}

# --- CONSTANTS & DATA ---
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


# --- MODELS ---
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
    settlementCoin: str = "RLUSD"
    incoterm: str = "FOB"
    supplierName: str
    supplierBankName: str = "Bank of China"
    supplierAccountNumber: str = "621700123456789"
    supplierSwiftCode: str = "BKCHCNBJ"
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


# --- HELPERS ---
def sha256_hex(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode("utf-8")).hexdigest().upper()


def load_db():
    base = {
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


def save_db(data):
    with _db_io_lock:
        with open(DB_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)


def record_fee(db, ftype, ref_id, usd, xhash):
    db["corporateRevenue"].append(
        {
            "id": f"FEE-{str(int(time.time()*1000))[-5:]}",
            "corporateWallet": CORPORATE_FEE_WALLET,
            "feeType": ftype,
            "referenceId": ref_id,
            "amountUsd": round(usd, 2),
