"""
KIS Open Trading API Collector
domestic_stock 怨듭떇 ?덉젣 湲곕컲
"""
import requests
import time
import os
import threading
from datetime import datetime, timedelta
from config.settings import KIS_DOMAIN, KIS_APP_KEY, KIS_APP_SECRET
from utils.helpers import get_kis_token

try:
    from server.monitoring import observe_kis_call, is_kis_degraded
except Exception:
    def observe_kis_call(success: bool, latency_sec: float):
        return None
    def is_kis_degraded(*args, **kwargs):
        return False


def safe_float(val, default=0.0):
    try:
        if val is None or str(val).strip() in ('', 'nan', 'NaN', '-'):
            return default
        return float(str(val).replace(',', ''))
    except Exception:
        return default


def safe_int(val, default=0):
    try:
        return int(safe_float(val, default))
    except Exception:
        return default


def safe_str(val, default="-"):
    if val is None or str(val).strip() in ('', 'nan', 'NaN'):
        return default
    return str(val).strip()


_RT_TR_IDS = {
    "FHKST01010200",  # orderbook
    "FHPST01060000",  # ticks
    "FHPST01390000",  # VI status
}
_DEGRADE_TARGET_TR_IDS = set(_RT_TR_IDS) | {"FHPST01710000"}  # ranking

_KIS_HTTP_CONCURRENCY_DEFAULT = max(1, int(os.getenv("KIS_HTTP_CONCURRENCY_DEFAULT", "12") or "12"))
_KIS_HTTP_CONCURRENCY_RT = max(1, int(os.getenv("KIS_HTTP_CONCURRENCY_RT", "6") or "6"))
_KIS_HTTP_SEM_DEFAULT = threading.BoundedSemaphore(_KIS_HTTP_CONCURRENCY_DEFAULT)
_KIS_HTTP_SEM_RT = threading.BoundedSemaphore(_KIS_HTTP_CONCURRENCY_RT)
_KIS_HTTP_SEM_ACQUIRE_TIMEOUT_DEFAULT = max(0.05, float(os.getenv("KIS_HTTP_SEM_ACQUIRE_TIMEOUT_DEFAULT_SEC", "0.8") or "0.8"))
_KIS_HTTP_SEM_ACQUIRE_TIMEOUT_RT = max(0.05, float(os.getenv("KIS_HTTP_SEM_ACQUIRE_TIMEOUT_RT_SEC", "0.4") or "0.4"))


class KISCollector:
    def __init__(self):
        self.base_url = KIS_DOMAIN
        # Compatibility attrs: some realtime paths read collector.app_key/app_secret directly.
        self.app_key = KIS_APP_KEY
        self.app_secret = KIS_APP_SECRET
        self.token = get_kis_token()
        self.headers = {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {self.token}",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
            "tr_id": "",
            "custtype": "P"
        }

    def _get(self, path, params, tr_id):
        headers = self.headers.copy()
        headers["tr_id"] = tr_id
        url = f"{self.base_url}{path}"
        timeout_default = float(os.getenv("KIS_HTTP_TIMEOUT_SEC", "4.0") or "4.0")
        timeout_rt = float(os.getenv("KIS_HTTP_TIMEOUT_RT_SEC", "2.0") or "2.0")
        tr = str(tr_id or "").strip()
        is_rt_call = tr in _RT_TR_IDS
        timeout_sec = timeout_rt if is_rt_call else timeout_default
        timeout_sec = max(1.0, timeout_sec)

        force_degraded = str(os.getenv("KIS_FORCE_DEGRADED", "0")).strip().lower() in ("1", "true", "yes", "on")
        degrade_auto_enabled = str(os.getenv("KIS_DEGRADE_AUTO_ENABLED", "1")).strip().lower() in ("1", "true", "yes", "on")
        degrade_apply_all = str(os.getenv("KIS_DEGRADE_APPLY_ALL", "0")).strip().lower() in ("1", "true", "yes", "on")
        if force_degraded:
            return {"rt_cd": "9", "error": "degraded_mode_forced"}
        if degrade_auto_enabled and (degrade_apply_all or tr in _DEGRADE_TARGET_TR_IDS):
            try:
                if is_kis_degraded():
                    return {"rt_cd": "9", "error": "degraded_mode_auto"}
            except Exception:
                pass

        sem = _KIS_HTTP_SEM_RT if is_rt_call else _KIS_HTTP_SEM_DEFAULT
        acquire_timeout = _KIS_HTTP_SEM_ACQUIRE_TIMEOUT_RT if is_rt_call else _KIS_HTTP_SEM_ACQUIRE_TIMEOUT_DEFAULT
        acquired = sem.acquire(timeout=acquire_timeout)
        if not acquired:
            return {"rt_cd": "9", "error": "local_concurrency_limited"}

        try:
            try:
                response = requests.get(url, headers=headers, params=params, timeout=timeout_sec)
                if response.status_code != 200:
                    return {"rt_cd": "9", "error": f"HTTP {response.status_code}"}

                data = response.json()
                if data.get("rt_cd") != "0":
                    msg1 = data.get("msg1", "")
                    return {"rt_cd": "9", "error": msg1}

                return data
            except requests.exceptions.Timeout:
                return {"rt_cd": "9", "error": "timeout"}
            except Exception as e:
                return {"rt_cd": "9", "error": str(e)}
        finally:
            sem.release()

    # 종목 마스터 — 신규 상장·재상장 종목명·시장구분 조회
    # TR: CTPF1002R  PRDT_TYPE_CD: 300 (주식/ETF/ETN/ELW)
    def get_stock_master(self, stock_code: str, prdt_type_cd: str = "300") -> dict:
        path = "/uapi/domestic-stock/v1/quotations/search-stock-info"
        params = {"PRDT_TYPE_CD": prdt_type_cd, "PDNO": stock_code}
        res = self._get(path, params, "CTPF1002R")
        output = res.get("output", {}) if isinstance(res, dict) else {}
        if not isinstance(output, dict):
            output = {}
        return {
            "name": safe_str(output.get("prdt_abrv_name") or output.get("prdt_name")),
            "name_eng": safe_str(output.get("prdt_eng_abrv_name")),
            "market": safe_str(output.get("mket_id_cd")),  # KSQ/STK/...
            "market_name": safe_str(output.get("rprs_mrkt_kor_name") or output.get("scrt_grp_cls_code")),
            "listing_date": safe_str(output.get("scts_mket_lstg_dt")),
            "delisting_date": safe_str(output.get("scts_mket_lstg_abol_dt")),
            "_raw": output,
        }

    # ?? [?꾩젽 1] ?꾩옱媛 ?쒖꽭 ??????????????????????????????????
    def get_price(self, stock_code: str) -> dict:
        path = "/uapi/domestic-stock/v1/quotations/inquire-price"
        params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": stock_code}
        res = self._get(path, params, "FHKST01010100")
        output = res.get("output", {})
        return {
            "current_price": safe_int(output.get("stck_prpr")),
            "open_price": safe_int(output.get("stck_oprc")), # ?쒓? 異붽?
            "change_pct": safe_float(output.get("prdy_ctrt")),
            "change_amt": safe_int(output.get("prdy_vrss")),
            "trading_value": safe_int(output.get("acml_tr_pbmn")),
            "volume_turnover_rate": safe_float(output.get("vol_tnrt")),
            "trading_volume": safe_int(output.get("acml_vol")),
            "market_cap": safe_int(output.get("hts_avls")),
            "per": safe_str(output.get("per")),
            "pbr": safe_str(output.get("pbr")),
            "eps": safe_str(output.get("eps")),
            "foreign_hold_pct": safe_float(output.get("hts_frgn_ehrt")),
            "listed_shares": safe_int(output.get("lstn_stcn")),
            "_raw": output
        }

    # ?? [?꾩젽 2] ?쇰퀎 OHLCV ???????????????????????????????????
    def get_daily_price(self, stock_code: str, period: str = "D") -> list:
        path = "/uapi/domestic-stock/v1/quotations/inquire-daily-price"
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": stock_code,
            "FID_PERIOD_DIV_CODE": period,
            "FID_ORG_ADJ_PRC": "1"
        }
        res = self._get(path, params, "FHKST01010400")
        raw_list = res.get("output", [])
        result = []
        for r in raw_list:
            result.append({
                "date": safe_str(r.get("stck_bsop_date")),
                "close": safe_int(r.get("stck_clpr")),
                "open": safe_int(r.get("stck_oprc")),
                "high": safe_int(r.get("stck_hgpr")),
                "low": safe_int(r.get("stck_lwpr")),
                "volume": safe_int(r.get("acml_vol")),
                "trading_value": safe_int(r.get("acml_tr_pbmn")),
                "credit_rate": safe_float(r.get("whol_loan_rmnd_rate")),
            })
        return result

    # ?? [?꾩젽 4] ?ъ옄???섍툒 ?쇰퀎 ?대젰 ???????????????????????
    # TR: FHKST01010900 ??output 諛곗뿴???좎쭨蹂??대젰 (理쒕? 20?쇱튂)
    def _parse_investor_row(self, r: dict) -> dict:
        """?ъ옄???섍툒 ?????좎쭨) ?꾩껜 ?뚯떛"""
        return {
            "date":             safe_str(r.get("stck_bsop_date")),
            # ?쒕ℓ??(net buy qty)
            "foreign":          safe_int(r.get("frgn_ntby_qty")),
            "institution":      safe_int(r.get("orgn_ntby_qty")),
            "individual":       safe_int(r.get("prsn_ntby_qty")),
            "etc_org":          safe_int(r.get("etc_orgn_ntby_qty", 0)),
            "program":          safe_int(r.get("pgm_ntby_qty", 0)),
            # ?쒕ℓ??嫄곕옒?湲?(??
            "foreign_net_amt":      safe_int(r.get("frgn_ntby_tr_pbmn", 0)),
            "institution_net_amt":  safe_int(r.get("orgn_ntby_tr_pbmn", 0)),
            "individual_net_amt":   safe_int(r.get("prsn_ntby_tr_pbmn", 0)),
            # 留ㅼ닔 嫄곕옒??            "foreign_buy":          safe_int(r.get("frgn_shnu_vol")),
            "institution_buy":      safe_int(r.get("orgn_shnu_vol")),
            "individual_buy":       safe_int(r.get("prsn_shnu_vol")),
            "etc_org_buy":          safe_int(r.get("etc_orgn_shnu_vol", 0)),
            "program_buy":          safe_int(r.get("pgm_shnu_vol", 0)),
            # 留ㅻ룄 嫄곕옒??            "foreign_sell":         safe_int(r.get("frgn_seln_vol")),
            "institution_sell":     safe_int(r.get("orgn_seln_vol")),
            "individual_sell":      safe_int(r.get("prsn_seln_vol")),
            "etc_org_sell":         safe_int(r.get("etc_orgn_seln_vol", 0)),
            "program_sell":         safe_int(r.get("pgm_seln_vol", 0)),
            # 留ㅼ닔 嫄곕옒?湲?(??
            "foreign_buy_amt":      safe_int(r.get("frgn_shnu_tr_pbmn", 0)),
            "institution_buy_amt":  safe_int(r.get("orgn_shnu_tr_pbmn", 0)),
            "individual_buy_amt":   safe_int(r.get("prsn_shnu_tr_pbmn", 0)),
            # 留ㅻ룄 嫄곕옒?湲?(??
            "foreign_sell_amt":     safe_int(r.get("frgn_seln_tr_pbmn", 0)),
            "institution_sell_amt": safe_int(r.get("orgn_seln_tr_pbmn", 0)),
            "individual_sell_amt":  safe_int(r.get("prsn_seln_tr_pbmn", 0)),
        }

    def get_member_trade(self, stock_code: str) -> dict:
        """종목별 거래원 매수/매도 Top — KIS inquire-member (FHKST01010500).

        응답 모양 (KIS 공식):
          output: {
            seln_qty_icdc1..5    (매도 1~5위 거래원 매도수량),
            shnu_qty_icdc1..5    (매수 1~5위 거래원 매수수량),
            seln_mbcr_no1..5     (매도 거래원 번호),
            shnu_mbcr_no1..5     (매수 거래원 번호),
            seln_mbcr_name1..5   (매도 거래원 이름),
            shnu_mbcr_name1..5   (매수 거래원 이름),
            byov_seln_qty1..5    (매도 변동량),
            byov_shnu_qty1..5    (매수 변동량),
            seln_mbcr_glob_yn_1..5  (매도 외국계 여부 Y/N),
            shnu_mbcr_glob_yn_1..5  (매수 외국계 여부 Y/N),
            ...
          }
        반환: {"buy": [...], "sell": [...]} — 각 5개 dict (rank, broker, qty, is_foreign).
        """
        path = "/uapi/domestic-stock/v1/quotations/inquire-member"
        params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": stock_code}
        res = self._get(path, params, "FHKST01010500")
        if res.get("rt_cd") != "0":
            return {"error": res.get("msg1") or "kis_failed", "buy": [], "sell": []}
        out = res.get("output") or {}
        if not out:
            return {"error": "empty_output", "buy": [], "sell": []}

        def _row(side: str, rank: int) -> dict:
            # side='buy': shnu_*, side='sell': seln_*
            prefix = "shnu" if side == "buy" else "seln"
            name = safe_str(out.get(f"{prefix}_mbcr_name{rank}"))
            no = safe_str(out.get(f"{prefix}_mbcr_no{rank}"))
            qty = safe_int(out.get(f"{prefix}_qty_icdc{rank}", 0))
            change = safe_int(out.get(f"byov_{prefix}_qty{rank}", 0))
            global_yn = safe_str(out.get(f"{prefix}_mbcr_glob_yn_{rank}", "N"))
            if not name:
                return None
            return {
                "rank": rank,
                "broker_name": name,
                "broker_no": no,
                "qty": qty,
                "qty_change": change,
                "is_foreign": global_yn == "Y",
            }

        buy_list = [r for rank in range(1, 6) for r in [_row("buy", rank)] if r]
        sell_list = [r for rank in range(1, 6) for r in [_row("sell", rank)] if r]
        return {
            "code": stock_code,
            "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "buy": buy_list,
            "sell": sell_list,
        }

    def get_investor_history(self, stock_code: str, max_days: int = 20) -> list:
        """?ъ옄???섍툒 ?쇰퀎 ?대젰 (理쒕? max_days?쇱튂) ??FHKST01010900 output 諛곗뿴 ?꾩껜"""
        path = "/uapi/domestic-stock/v1/quotations/inquire-investor"
        params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": stock_code}
        res = self._get(path, params, "FHKST01010900")
        raw_list = res.get("output", [])
        if isinstance(raw_list, dict):
            raw_list = [raw_list]
        if not isinstance(raw_list, list):
            return []
        # _parse_investor_row 결과에 기관 세분화 6종(은행/보험/투신/연기금/사모/기타금융)
        # 을 raw 응답에서 직접 추출해 머지. KIS 응답에 들어오는 필드:
        #   bank_ntby_qty, inse_cmpn_ntby_qty, ivst_co_ntby_qty,
        #   pen_fund_ntby_qty, prvt_fund_ntby_qty, etc_fnnc_ntby_qty
        # 일부 종목/일자엔 None 가능 → safe_int 0 처리.
        out = []
        for r in raw_list[:max_days]:
            row = self._parse_investor_row(r)
            row["bank"]         = safe_int(r.get("bank_ntby_qty", 0))
            row["insurance"]    = safe_int(r.get("inse_cmpn_ntby_qty", 0))
            row["trust"]        = safe_int(r.get("ivst_co_ntby_qty", 0))
            row["pension"]      = safe_int(r.get("pen_fund_ntby_qty", 0))
            row["private_fund"] = safe_int(r.get("prvt_fund_ntby_qty", 0))
            row["etc_finance"]  = safe_int(r.get("etc_fnnc_ntby_qty", 0))
            out.append(row)
        return out

    def get_investor_today(self, stock_code: str) -> dict:
        """?뱀씪 ?ъ옄???섍툒 (?대젰??泥?踰덉㎏)"""
        rows = self.get_investor_history(stock_code, max_days=1)
        return rows[0] if rows else {}

    def get_investor_5d(self, stock_code: str) -> list:
        """Deprecated placeholder; handled by investor_history pipeline."""
        return []

    # ?? [?꾩젽 4] ?꾨줈洹몃옩 留ㅻℓ ?쇰퀎 ???????????????????????????
    def get_program_trade_5d(self, stock_code: str) -> list:
        path = "/uapi/domestic-stock/v1/quotations/program-trade-by-stock-daily"
        params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": stock_code, "FID_INPUT_DATE_1": ""}
        res = self._get(path, params, "FHPPG04650201")
        raw_list = res.get("output", [])
        result = []
        for r in raw_list[:5]:
            program_buy = safe_int(r.get("pgm_shnu_vol"))
            if program_buy <= 0:
                program_buy = safe_int(r.get("whol_smtn_shnu_vol"))

            program_sell = safe_int(r.get("pgm_seln_vol"))
            if program_sell <= 0:
                program_sell = safe_int(r.get("whol_smtn_seln_vol"))

            program_net = safe_int(r.get("pgm_ntby_qty"))
            if program_net == 0:
                program_net = safe_int(r.get("whol_smtn_ntby_qty"))

            program_net_amt = safe_int(r.get("pgm_ntby_tr_pbmn"))
            if program_net_amt == 0:
                program_net_amt = safe_int(r.get("whol_smtn_ntby_tr_pbmn"))

            result.append({
                "date": safe_str(r.get("stck_bsop_date")),
                "program_buy": program_buy,
                "program_sell": program_sell,
                "program_net": program_net,
                "program_net_amt": program_net_amt,
            })
        return result

    # ?? [?꾩젽 A] 怨듬ℓ???쇰퀎 異붿씠 ?????????????????????????????
    def get_short_sale(self, stock_code: str, days: int = 5) -> dict:
        path = "/uapi/domestic-stock/v1/quotations/daily-short-sale"
        end_date = datetime.now().strftime("%Y%m%d")
        start_date = (datetime.now() - timedelta(days=days * 2)).strftime("%Y%m%d")
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": stock_code,
            "FID_INPUT_DATE_1": start_date,
            "FID_INPUT_DATE_2": end_date
        }
        res = self._get(path, params, "FHPST04830000")
        out1 = res.get("output1", {})
        out2_list = res.get("output2", [])
        if isinstance(out2_list, dict):
            out2_list = [out2_list]
        if not isinstance(out2_list, list):
            out2_list = []

        summary_ratio = safe_float(out1.get("shrt_slng_rto"))
        if summary_ratio <= 0 and out2_list:
            summary_ratio = safe_float((out2_list[0] or {}).get("acml_ssts_cntg_qty_rlim"))

        daily = []
        for r in out2_list[:days]:
            short_volume = safe_int(r.get("shrt_slng_qty"))
            if short_volume <= 0:
                short_volume = safe_int(r.get("ssts_cntg_qty"))

            short_ratio = safe_float(r.get("shrt_slng_rto"))
            if short_ratio <= 0:
                short_ratio = safe_float(r.get("ssts_vol_rlim"))
            if short_ratio <= 0:
                short_ratio = safe_float(r.get("acml_ssts_cntg_qty_rlim"))

            loan_balance = safe_int(r.get("loan_rmnd_qty"))
            if loan_balance <= 0:
                loan_balance = safe_int(r.get("acml_ssts_cntg_qty"))

            daily.append({
                "date": safe_str(r.get("stck_bsop_date")),
                "short_volume": short_volume,
                "short_ratio": short_ratio,
                "loan_balance": loan_balance,
                "close": safe_int(r.get("stck_clpr")),
            })
        return {"short_selling_volume_ratio": summary_ratio, "daily": daily}

    # ?? [?꾩젽 B] ?좎슜?붽퀬 ?쇰퀎 ????????????????????????????????
    def get_credit_balance(self, stock_code: str) -> list:
        path = "/uapi/domestic-stock/v1/quotations/daily-credit-balance"
        today = datetime.now().strftime("%Y%m%d")
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_COND_SCR_DIV_CODE": "20476",
            "FID_INPUT_ISCD": stock_code,
            "FID_INPUT_DATE_1": today
        }
        res = self._get(path, params, "FHPST04760000")
        raw_list = res.get("output", [])
        if isinstance(raw_list, dict):
            raw_list = [raw_list]
        if not isinstance(raw_list, list):
            raw_list = []

        result = []
        for r in raw_list[:5]:
            date_value = safe_str(r.get("stck_bsop_date"))
            if date_value == "-":
                date_value = safe_str(r.get("deal_date"))

            credit_rate = safe_float(r.get("crdt_rmnd_itre_rate"))
            if credit_rate <= 0:
                credit_rate = safe_float(r.get("whol_loan_rmnd_rate"))

            credit_qty = safe_int(r.get("crdt_rmnd_qty"))
            if credit_qty <= 0:
                credit_qty = safe_int(r.get("whol_loan_rmnd_stcn"))

            repay_qty = safe_int(r.get("stck_loan_rpy_qty"))
            if repay_qty <= 0:
                repay_qty = safe_int(r.get("whol_loan_rdmp_stcn"))

            result.append({
                "date": date_value,
                "credit_rate": credit_rate,
                "credit_qty": credit_qty,
                "repay_qty": repay_qty,
            })
        return result

    # ?? [?꾩젽 6] ?щТ鍮꾩쑉 ?????????????????????????????????????
    def get_finance_ratio(self, stock_code: str) -> dict:
        path = "/uapi/domestic-stock/v1/finance/financial-ratio"
        params = {"FID_DIV_CLS_CODE": "0", "fid_cond_mrkt_div_code": "J", "fid_input_iscd": stock_code}
        res = self._get(path, params, "FHKST66430300")
        raw_list = res.get("output", [])
        if not raw_list:
            return {"debt_ratio": "-", "retention_ratio": "-", "roe": "-", "roa": "-"}
        r = raw_list[0] if isinstance(raw_list, list) else raw_list
        # Some symbols return roe_val/ntin_inrt instead of stck_roe/stck_roa.
        roe = r.get("stck_roe")
        if roe in (None, "", "nan", "NaN", "-"):
            roe = r.get("roe_val")
        roa = r.get("stck_roa")
        if roa in (None, "", "nan", "NaN", "-"):
            roa = r.get("roa_val")
        return {
            "debt_ratio": safe_str(r.get("lblt_rate")),
            "retention_ratio": safe_str(r.get("rsrv_rate")),
            "roe": safe_str(roe),
            "roa": safe_str(roa),
            "bps": safe_str(r.get("bps")),
            "eps": safe_str(r.get("eps")),
        }

    # ?? [?꾩젽 3] 吏???쇰퀎 ????????????????????????????????????
    def get_index_daily(self, index_code: str) -> list:
        path = "/uapi/domestic-stock/v1/quotations/inquire-daily-indexchartprice"
        params = {
            "FID_COND_MRKT_DIV_CODE": "U",
            "FID_INPUT_ISCD": index_code,
            "FID_INPUT_DATE_1": "",
            "FID_INPUT_DATE_2": "",
            "FID_PERIOD_DIV_CODE": "D"
        }
        res = self._get(path, params, "FHPUP02120000")
        raw_list = res.get("output2", [])
        result = []
        for r in raw_list[:30]:
            result.append({
                "date": safe_str(r.get("stck_bsop_date")),
                "close": safe_float(r.get("bstp_nmix_prpr")),
                "change_pct": safe_float(r.get("bstp_nmix_prdy_ctrt")),
            })
        return result

    # ?? [?꾩젽 C] 嫄곕옒?湲??쒖쐞 ????????????????????????????????
    def get_transaction_value_ranking(self, market_code: str = "0001") -> list:
        """
        援?궡二쇱떇 嫄곕옒?湲??쒖쐞 (FHPST01710000)
        market_code: '0000'(?꾩껜), '0001'(肄붿뒪??, '1001'(肄붿뒪??, '2001'(肄붿뒪??00)
        """
        path = "/uapi/domestic-stock/v1/quotations/volume-rank"
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_COND_SCR_DIV_CODE": "20171",
            "FID_INPUT_ISCD": market_code,
            "FID_DIV_CLS_CODE": "0",
            "FID_BLNG_CLS_CODE": "0",
            "FID_TRGT_CLS_CODE": "0",
            "FID_TRGT_EXCL_CLS_CODE": "0",
            "FID_VOL_CNT": "100",
            "FID_INPUT_PRICE_1": "0",
            "FID_INPUT_PRICE_2": "0"
        }
        res = self._get(path, params, "FHPST01710000")
        if not res or 'output' not in res:
            return []
            
        raw_list = res.get("output", [])
        result = []
        for r in raw_list:
            result.append({
                "rank": safe_int(r.get("data_rank")),
                "code": safe_str(r.get("mksc_shrn_iscd") or r.get("stck_shrn_iscd"), default=None),
                "name": safe_str(r.get("hts_kor_isnm")),
                "close": safe_int(r.get("stck_prpr")),
                "change_amt": safe_int(r.get("prdy_vrss")),
                "change_pct": safe_float(r.get("prdy_ctrt")),
                "volume": safe_int(r.get("acml_vol")),
                "trading_value": safe_int(r.get("acml_tr_pbmn")),  # ???⑥쐞 (怨깆뀍 遺덊븘??
            })
        return result

    # ?? [?좉퇋] ?щТ?쒗몴 ???먯씡怨꾩궛???????????????????????????????
    def get_income_statement(self, stock_code: str) -> list:
        """?먯씡怨꾩궛??5媛쒕뀈 (FHKST66430100)"""
        path = "/uapi/domestic-stock/v1/finance/income-statement"
        params = {"FID_DIV_CLS_CODE": "1", "fid_cond_mrkt_div_code": "J", "fid_input_iscd": stock_code}
        res = self._get(path, params, "FHKST66430100")
        raw_list = res.get("output", [])
        if not isinstance(raw_list, list):
            raw_list = [raw_list] if raw_list else []
        result = []
        for r in raw_list[:5]:
            result.append({
                "year": safe_str(r.get("stac_yymm")),
                "revenue": safe_int(r.get("sale_account")),
                "operating_profit": safe_int(r.get("bsop_prti")),
                "net_income": safe_int(r.get("thtr_ntin")),
                "op_margin": safe_float(r.get("bsop_prfi_rate")),
                "net_margin": safe_float(r.get("thtr_ntin_rate")),
            })
        return result

    # ?? [?좉퇋] ?щТ?쒗몴 ???李⑤?議고몴 ?????????????????????????????
    def get_balance_sheet(self, stock_code: str) -> list:
        """?李⑤?議고몴 5媛쒕뀈 (FHKST66430200)"""
        path = "/uapi/domestic-stock/v1/finance/balance-sheet"
        params = {"FID_DIV_CLS_CODE": "1", "fid_cond_mrkt_div_code": "J", "fid_input_iscd": stock_code}
        res = self._get(path, params, "FHKST66430200")
        raw_list = res.get("output", [])
        if not isinstance(raw_list, list):
            raw_list = [raw_list] if raw_list else []
        result = []
        for r in raw_list[:5]:
            result.append({
                "year": safe_str(r.get("stac_yymm")),
                "total_assets": safe_int(r.get("total_aset")),
                "total_liabilities": safe_int(r.get("total_lblt")),
                "total_equity": safe_int(r.get("total_cptl")),
                "debt_ratio": safe_float(r.get("lblt_rate")),
            })
        return result

    # ?? [?좉퇋] ?ъ옄?섍껄 ????????????????????????????????????????
    def get_invest_opinion(self, stock_code: str) -> list:
        """利앷텒???ъ옄?섍껄 由ъ뒪??(FHKST66430400)"""
        path = "/uapi/domestic-stock/v1/finance/invest-opinion"
        today = datetime.now().strftime("%Y%m%d")
        start = (datetime.now() - timedelta(days=90)).strftime("%Y%m%d")
        params = {
            "FID_INPUT_ISCD": stock_code,
            "FID_INPUT_DATE_1": start,
            "FID_INPUT_DATE_2": today,
        }
        res = self._get(path, params, "FHKST66430400")
        raw_list = res.get("output", [])
        if not isinstance(raw_list, list):
            raw_list = [raw_list] if raw_list else []
        result = []
        for r in raw_list[:20]:
            result.append({
                "date": safe_str(r.get("stck_bsop_date")),
                "broker": safe_str(r.get("mbcr_name")),
                "opinion": safe_str(r.get("invt_opnn")),
                "target_price": safe_int(r.get("tagt_prc")),
                "close": safe_int(r.get("stck_prpr")),
            })
        return result

    # ?? [?좉퇋] ?깅씫瑜??쒖쐞 ????????????????????????????????????
    def get_fluctuation_rank(self, market_code: str = "0001", is_up: bool = True) -> list:
        """?깅씫瑜??곸쐞/?섏쐞 醫낅ぉ (FHPST01420000)"""
        path = "/uapi/domestic-stock/v1/quotations/fluctuation-rank"
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_COND_SCR_DIV_CODE": "20141",
            "FID_INPUT_ISCD": market_code,
            "FID_RANK_SORT_CLS_CODE": "0" if is_up else "1",
            "FID_INPUT_CNT_1": "0",
            "FID_PRCBD_DIV_CODE": "0",
            "FID_INPUT_PRICE_1": "0",
            "FID_INPUT_PRICE_2": "0",
            "FID_VOL_CNT": "0",
            "FID_TRGT_CLS_CODE": "0",
            "FID_TRGT_EXCL_CLS_CODE": "0",
        }
        res = self._get(path, params, "FHPST01420000")
        raw_list = res.get("output", [])
        if not isinstance(raw_list, list):
            raw_list = []
        result = []
        for r in raw_list[:50]:
            result.append({
                "rank": safe_int(r.get("data_rank")),
                "code": safe_str(r.get("mksc_shrn_iscd") or r.get("stck_shrn_iscd"), default=None),
                "name": safe_str(r.get("hts_kor_isnm")),
                "close": safe_int(r.get("stck_prpr")),
                "change_pct": safe_float(r.get("prdy_ctrt")),
                "change_amt": safe_int(r.get("prdy_vrss")),
                "volume": safe_int(r.get("acml_vol")),
                "trading_value": safe_int(r.get("acml_tr_pbmn")),
            })
        return result

    # ?? [?좉퇋] VI 諛쒕룞 ?꾪솴 ????????????????????????????????????
    def get_vi_status(self) -> dict:
        """VI status snapshot. Returns {'items': [...], 'error': None|str}."""
        from datetime import date

        today = date.today().strftime("%Y%m%d")
        path = "/uapi/domestic-stock/v1/quotations/inquire-vi-status"
        params = {
            "FID_DIV_CLS_CODE": "0",
            "FID_COND_SCR_DIV_CODE": "20139",
            "FID_MRKT_CLS_CODE": "0",
            "FID_INPUT_ISCD": "",
            "FID_RANK_SORT_CLS_CODE": "0",
            "FID_INPUT_DATE_1": today,
            "FID_TRGT_CLS_CODE": "",
            "FID_TRGT_EXLS_CLS_CODE": "",
        }
        res = self._get(path, params, "FHPST01390000")

        if res.get("rt_cd") == "9":
            err = res.get("error") or res.get("msg1") or "KIS API error"
            return {"items": [], "error": err}

        raw_list = res.get("output", [])
        if isinstance(raw_list, dict):
            raw_list = [raw_list] if raw_list.get("mksc_shrn_iscd") else []
        if not isinstance(raw_list, list):
            raw_list = []

        items = []
        for r in raw_list:
            code = safe_str(r.get("mksc_shrn_iscd"))
            if code == "-":
                continue
            items.append({
                "code": code,
                "name": safe_str(r.get("hts_kor_isnm")),
                "vi_active": safe_str(r.get("vi_cls_code")),
                "vi_kind": safe_str(r.get("vi_kind_code")),
                "vi_time": safe_str(r.get("cntg_vi_hour")),
                "vi_cancel_time": safe_str(r.get("vi_cncl_hour")),
                "vi_price": safe_int(r.get("vi_prc")),
                "static_std_prc": safe_int(r.get("vi_stnd_prc")),
                "static_dprt": safe_float(r.get("vi_dprt")),
                "dynamic_std_prc": safe_int(r.get("vi_dmc_stnd_prc")),
                "dynamic_dprt": safe_float(r.get("vi_dmc_dprt")),
                "vi_count": safe_int(r.get("vi_count")),
                "date": safe_str(r.get("bsop_date")),
            })
        return {"items": items, "error": None}

    def get_night_futures_price(self, srs_cd: str) -> dict:
        """
        CME ?곌퀎 ?쇨컙?좊Ъ ?꾩옱媛 議고쉶
        TR_ID: HHDFC55010000
        srs_cd: fo_cme_code.mst?먯꽌 ?뚯떛???⑥텞肄붾뱶 (?? 101S9)
        """
        path = "/uapi/overseas-futures/v1/quotations/inquire-price"
        params = {"SRS_CD": srs_cd}
        res = self._get(path, params, "HHDFC55010000")
        output = res.get("output", {})
        if not output:
            return {"error": res.get("error", "no data"), "srs_cd": srs_cd}
        return {
            "srs_cd":      srs_cd,
            "name":        safe_str(output.get("hts_kor_isnm")),
            "close":       safe_float(output.get("ovrs_nmix_prpr")),
            "open":        safe_float(output.get("ovrs_nmix_oprc")),
            "high":        safe_float(output.get("ovrs_nmix_hgpr")),
            "low":         safe_float(output.get("ovrs_nmix_lwpr")),
            "change":      safe_float(output.get("ovrs_nmix_prdy_vrss")),
            "change_pct":  safe_float(output.get("prdy_ctrt")),
            "volume":      safe_int(output.get("acml_vol")),
            "updated_at":  datetime.now().isoformat(),
            "error":       None,
        }

    # ?? [?쇨컙?좊Ъ] CME ?곌퀎 ?쇨컙?좊Ъ ?쇰큺 李⑦듃 ????????????????????
    def get_night_futures_chart(self, srs_cd: str, days: int = 60) -> list:
        """
        CME ?곌퀎 ?쇨컙?좊Ъ ?쇰큺 OHLCV 議고쉶
        TR_ID: HHDFC52100100
        """
        path = "/uapi/overseas-futures/v1/quotations/inquire-daily-chartprice"
        end_dt   = datetime.now().strftime("%Y%m%d")
        start_dt = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
        params = {
            "SRS_CD":           srs_cd,
            "GUNTN_STRT_DATE":  start_dt,
            "GUNTN_END_DATE":   end_dt,
            "PERIOD_DIV_CODE":  "D",
        }
        res = self._get(path, params, "HHDFC52100100")
        raw_list = res.get("output2", [])
        if not isinstance(raw_list, list):
            raw_list = []
        result = []
        for r in raw_list:
            close = safe_float(r.get("ovrs_nmix_prpr") or r.get("stck_clpr"))
            if close <= 0:
                continue
            result.append({
                "date":   safe_str(r.get("bass_dt") or r.get("stck_bsop_date")),
                "close":  close,
                "open":   safe_float(r.get("ovrs_nmix_oprc") or r.get("stck_oprc")),
                "high":   safe_float(r.get("ovrs_nmix_hgpr") or r.get("stck_hgpr")),
                "low":    safe_float(r.get("ovrs_nmix_lwpr") or r.get("stck_lwpr")),
                "volume": safe_int(r.get("acml_vol")),
            })
        # ?좎쭨 ?ㅻ쫫李⑥닚 ?뺣젹
        result.sort(key=lambda x: x["date"])
        return result

