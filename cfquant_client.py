"""
cfquant REST / WebSocket 客户端
用于通过 cfquant 服务直接与大 QMT 通信，全面平替原 MiniQMT / CurveZMQ。
"""

import logging
import json
import time
from typing import Optional, Dict, Any, List, Union
import httpx

logger = logging.getLogger(__name__)


def normalize_stock_code(symbol: str) -> str:
    """标准化证券代码为 000001.SZ / 600000.SH 格式"""
    raw = str(symbol or "").strip().upper()
    if not raw:
        return ""
    if "." in raw:
        parts = raw.split(".")
        code, market = parts[0], parts[1]
        if code.isdigit():
            code = code.zfill(6)
        if market not in ("SH", "SZ", "BJ"):
            market = "SH" if code.startswith(("6", "5", "9")) else "SZ"
        return f"{code}.{market}"
    
    if raw.isdigit():
        code = raw.zfill(6)
        market = "SH" if code.startswith(("6", "5", "9")) else "SZ"
        return f"{code}.{market}"
    return raw


class CFQuantClient:
    """cfquant HTTP API 客户端"""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8765",
        api_key: Optional[str] = None,
        account_id: Optional[str] = None,
        account_type: str = "STOCK",
        timeout: float = 30.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.account_id = str(account_id or "").strip()
        self.account_type = account_type.upper() if account_type else "STOCK"
        self.timeout = timeout
        self._client: Optional[httpx.Client] = None

    @property
    def client(self) -> httpx.Client:
        if self._client is None or self._client.is_closed:
            headers = {
                "Accept": "application/json",
                "User-Agent": "QuantTrader-cfquant/1.0",
            }
            if self.api_key:
                headers["X-API-Key"] = self.api_key
            self._client = httpx.Client(
                base_url=self.base_url,
                headers=headers,
                timeout=httpx.Timeout(self.timeout, connect=10.0),
                follow_redirects=True,
            )
        return self._client

    def close(self):
        if self._client and not self._client.is_closed:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        json_data: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """统一请求执行，带重试与异常封装"""
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        req_timeout = timeout or self.timeout
        
        headers = {}
        if self.api_key:
            headers["X-API-Key"] = self.api_key

        for attempt in range(2):
            try:
                resp = self.client.request(
                    method=method,
                    url=url,
                    params=params,
                    json=json_data,
                    headers=headers,
                    timeout=req_timeout,
                )
                
                # 检查 HTTP 状态码
                if resp.status_code >= 400:
                    logger.error(
                        f"cfquant 请求失败 [{resp.status_code}] {method} {url}: {resp.text[:300]}"
                    )
                    return {"ok": False, "status": resp.status_code, "error": resp.text}
                
                data = resp.json()
                return data
            except (httpx.ConnectError, httpx.ReadTimeout) as e:
                logger.warning(f"cfquant 网络重试 (第 {attempt+1} 次): {e}")
                if attempt == 1:
                    logger.error(f"cfquant 请求异常 {method} {url}: {e}")
                    return {"ok": False, "error": str(e)}
                time.sleep(0.5)
            except Exception as e:
                logger.error(f"cfquant 未知请求异常 {method} {url}: {e}")
                return {"ok": False, "error": str(e)}
        return {"ok": False, "error": "Max retries exceeded"}

    # ==================== 系统与健康检查 ====================

    def ping(self) -> bool:
        """测试服务是否可用"""
        try:
            # 查一下账号资金即可确认端到端可用
            res = self.get_asset()
            return res.get("ok", False)
        except Exception:
            return False

    def is_connected(self) -> bool:
        """检查连接状态"""
        return self.ping()

    # ==================== 账户与资产 ====================

    def get_account_data(
        self,
        options: str = "asset",
        account_id: Optional[str] = None,
        account_type: Optional[str] = None,
        force: int = 1,
    ) -> Dict[str, Any]:
        """
        获取账户数据: asset, positions, orders, trades
        GET /api/account
        """
        acc_id = str(account_id or self.account_id or "").strip()
        acc_type = (account_type or self.account_type or "STOCK").upper()
        
        params = {
            "account_id": acc_id,
            "account_type": acc_type,
            "options": options,
            "force": force,
        }
        res = self._request("GET", "/api/account", params=params)
        return res

    def get_asset(
        self,
        account_id: Optional[str] = None,
        account_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        """获取资产"""
        res = self.get_account_data(options="asset", account_id=account_id, account_type=account_type)
        if res.get("ok") and isinstance(res.get("data"), dict):
            asset_info = res["data"].get("asset", {})
            if asset_info.get("ok") and asset_info.get("data"):
                items = asset_info["data"]
                return {"ok": True, "data": items[0] if isinstance(items, list) and items else {}}
        return {"ok": False, "data": {}, "error": res.get("error", "Failed to fetch asset")}

    def get_positions(
        self,
        account_id: Optional[str] = None,
        account_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """获取当前持仓列表"""
        res = self.get_account_data(options="positions", account_id=account_id, account_type=account_type)
        if res.get("ok") and isinstance(res.get("data"), dict):
            pos_info = res["data"].get("positions", {})
            if pos_info.get("ok") and isinstance(pos_info.get("data"), list):
                return pos_info["data"]
        return []

    def get_orders(
        self,
        account_id: Optional[str] = None,
        account_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """获取当日委托列表"""
        res = self.get_account_data(options="orders", account_id=account_id, account_type=account_type)
        if res.get("ok") and isinstance(res.get("data"), dict):
            ord_info = res["data"].get("orders", {})
            if ord_info.get("ok") and isinstance(ord_info.get("data"), list):
                return ord_info["data"]
        return []

    def get_trades(
        self,
        account_id: Optional[str] = None,
        account_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """获取当日成交记录"""
        res = self.get_account_data(options="trades", account_id=account_id, account_type=account_type)
        if res.get("ok") and isinstance(res.get("data"), dict):
            trd_info = res["data"].get("trades", {})
            if trd_info.get("ok") and isinstance(trd_info.get("data"), list):
                return trd_info["data"]
        return []

    # ==================== 下单与撤单 ====================

    def build_order_confirm_text(
        self,
        side: str,
        stock_code: str,
        volume: Union[int, float],
        price: Optional[float] = None,
    ) -> str:
        """生成标准下单 confirm_text: BUY 000001.SZ 100 @ 10.000"""
        action = side.upper()
        norm_code = normalize_stock_code(stock_code)
        p = float(price or 0.0)
        v = int(volume)
        return f"{action} {norm_code} {v} @ {p:.3f}"

    def build_cancel_confirm_text(self, order_id: Union[str, int]) -> str:
        """生成标准撤单 confirm_text: CANCEL 1001"""
        return f"CANCEL {str(order_id).strip()}"

    def place_order(
        self,
        stock_code: str,
        side: str,
        volume: Union[int, float],
        price: Optional[float] = None,
        price_type: Union[int, str] = 11,  # 11: 限价 (FIX_PRICE), 50: 最新价/市价
        account_id: Optional[str] = None,
        account_type: Optional[str] = None,
        order_action: Optional[str] = None,
        confirm_text: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        单笔委托下单
        POST /api/order
        """
        acc_id = str(account_id or self.account_id or "").strip()
        acc_type = (account_type or self.account_type or "STOCK").upper()
        norm_code = normalize_stock_code(stock_code)
        
        if not confirm_text:
            confirm_text = self.build_order_confirm_text(side, norm_code, volume, price)
        
        payload = {
            "account_id": acc_id,
            "account_type": acc_type,
            "side": side.lower(),
            "stock_code": norm_code,
            "price_type": price_type,
            "price": float(price or 0.0),
            "volume": int(volume),
            "confirm_text": confirm_text,
            "timeout": int(self.timeout),
        }
        if order_action:
            payload["order_action"] = order_action

        return self._request("POST", "/api/order", json_data=payload)

    def place_batch_orders(
        self,
        orders: List[Dict[str, Any]],
        account_id: Optional[str] = None,
        account_type: Optional[str] = None,
        stop_on_error: bool = False,
    ) -> Dict[str, Any]:
        """
        批量委托下单
        POST /api/orders/batch
        """
        acc_id = str(account_id or self.account_id or "").strip()
        acc_type = (account_type or self.account_type or "STOCK").upper()
        
        norm_orders = []
        for o in orders:
            norm_orders.append({
                "stock_code": normalize_stock_code(o["stock_code"]),
                "price": float(o.get("price", 0.0)),
                "volume": int(o.get("volume", o.get("quantity", 0))),
                "order_type": o.get("order_type", 23),
                "price_type": o.get("price_type", 11),
            })
            
        payload = {
            "account_id": acc_id,
            "account_type": acc_type,
            "orders_json": json.dumps(norm_orders),
            "confirm_text": f"BATCH {len(norm_orders)}",
            "stop_on_error": stop_on_error,
            "timeout": int(self.timeout),
        }
        return self._request("POST", "/api/orders/batch", json_data=payload)

    def cancel_order(
        self,
        order_id: Union[str, int],
        account_id: Optional[str] = None,
        account_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        撤单
        POST /api/cancel
        """
        acc_id = str(account_id or self.account_id or "").strip()
        acc_type = (account_type or self.account_type or "STOCK").upper()
        oid = str(order_id).strip()
        
        payload = {
            "account_id": acc_id,
            "account_type": acc_type,
            "order_id": oid,
            "confirm_text": self.build_cancel_confirm_text(oid),
            "timeout": int(self.timeout),
        }
        return self._request("POST", "/api/cancel", json_data=payload)

    # ==================== 行情与数据 ====================

    def get_full_tick(self, symbols: Union[str, List[str]]) -> Dict[str, Any]:
        """
        获取实时 Tick 快照 (五档)
        POST /api/data/full-tick
        """
        if isinstance(symbols, str):
            code_list = [normalize_stock_code(s.strip()) for s in symbols.split(",") if s.strip()]
        else:
            code_list = [normalize_stock_code(s) for s in symbols]
            
        payload = {"code_list": code_list, "timeout": int(self.timeout)}
        res = self._request("POST", "/api/data/full-tick", json_data=payload)
        if res.get("ok") and isinstance(res.get("data"), dict):
            return res["data"].get("result", {})
        return {}

    def get_market_data(
        self,
        symbols: Union[str, List[str]],
        period: str = "1d",
        start_time: str = "",
        end_time: str = "",
        count: int = -1,
        dividend_type: str = "none",
    ) -> Dict[str, Any]:
        """
        获取 K 线数据
        POST /api/data/market
        """
        if isinstance(symbols, str):
            stock_list = [normalize_stock_code(s.strip()) for s in symbols.split(",") if s.strip()]
        else:
            stock_list = [normalize_stock_code(s) for s in symbols]

        payload = {
            "stock_list": stock_list,
            "period": period,
            "start_time": start_time,
            "end_time": end_time,
            "count": count,
            "dividend_type": dividend_type,
            "timeout": int(self.timeout),
        }
        res = self._request("POST", "/api/data/market", json_data=payload)
        if res.get("ok") and isinstance(res.get("data"), dict):
            return res["data"].get("result", {})
        return {}

    def download_history_data(
        self,
        symbol: str,
        period: str = "1d",
        start_time: str = "",
        end_time: str = "",
    ) -> Dict[str, Any]:
        """
        触发历史行情下载
        POST /api/data/history/download
        """
        payload = {
            "stock_code": normalize_stock_code(symbol),
            "period": period,
            "start_time": start_time,
            "end_time": end_time,
            "timeout": int(self.timeout),
        }
        return self._request("POST", "/api/data/history/download", json_data=payload)

    def get_sector_stocks(self, sector_name: str = "沪深A股") -> List[str]:
        """
        查询板块成分股
        POST /api/data/sector
        """
        payload = {"sector_name": sector_name, "timeout": int(self.timeout)}
        res = self._request("POST", "/api/data/sector", json_data=payload)
        if res.get("ok") and isinstance(res.get("data"), dict):
            result = res["data"].get("result", [])
            if isinstance(result, list):
                return result
        return []

    def get_instrument_detail(self, symbol: str) -> Dict[str, Any]:
        """
        获取合约详情
        POST /api/data/instrument
        """
        payload = {
            "stock_code": normalize_stock_code(symbol),
            "iscomplete": "0",
            "timeout": int(self.timeout),
        }
        res = self._request("POST", "/api/data/instrument", json_data=payload)
        if res.get("ok") and isinstance(res.get("data"), dict):
            return res["data"].get("result", {})
        return {}

    def get_financial_data(
        self,
        symbol: str,
        table: str = "ASHAREBALANCESHEET",
        fields: str = "fix_assets",
        mode: str = "filled",
    ) -> Dict[str, Any]:
        """
        读取财务数据
        POST /api/data/financial
        """
        payload = {
            "stock_code": normalize_stock_code(symbol),
            "table": table,
            "fields": fields,
            "mode": mode,
            "timeout": int(self.timeout),
        }
        res = self._request("POST", "/api/data/financial", json_data=payload)
        if res.get("ok") and isinstance(res.get("data"), dict):
            return res["data"].get("result", {})
        return {}

    # ==================== 交易回调 ====================

    def get_callbacks(
        self,
        account_id: Optional[str] = None,
        account_type: Optional[str] = None,
        since: int = 0,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """
        拉取最新交易与委托回调事件
        GET /api/callbacks
        """
        acc_id = str(account_id or self.account_id or "").strip()
        acc_type = (account_type or self.account_type or "STOCK").upper()
        params = {
            "account_id": acc_id,
            "account_type": acc_type,
            "since": since,
            "limit": limit,
        }
        res = self._request("GET", "/api/callbacks", params=params)
        if res.get("ok") and isinstance(res.get("data"), dict):
            return res["data"].get("events", [])
        return []
