#!/usr/bin/env python3
"""
QMT cfquant CLI - 后端调试与运维控制台
基于 cfquant REST API 直接与大 QMT 交互
支持交互式命令、自动补全、表格化数据显示
"""

import os
import sys
import json
import html as _html
import logging
import time
import pandas as pd
from datetime import datetime
from pathlib import Path
from tabulate import tabulate
from prompt_toolkit import PromptSession, print_formatted_text as printft, HTML
from prompt_toolkit.history import FileHistory
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.completion import WordCompleter, Completer, Completion
from dotenv import load_dotenv

# 确保导入路径正确
current_dir = os.path.dirname(os.path.abspath(__file__))
if current_dir not in sys.path:
    sys.path.insert(0, current_dir)

from cfquant_client import CFQuantClient, normalize_stock_code

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("QMT-CLI")


class QMTCompleter(Completer):
    def __init__(self, cli):
        self.cli = cli
        self.cmd_completer = WordCompleter(list(self.cli.commands.keys()), ignore_case=True)
        self.sector_cache = None

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        words = text.split(" ")
        
        if len(words) <= 1:
            yield from self.cmd_completer.get_completions(document, complete_event)
            return

        # use 命令: 第二参数按账号前缀补全剩余部分
        if words[0].lower() == "use" and len(words) == 2:
            prefix = words[1]
            start_position = -len(prefix)
            for acc in self.cli.get_known_accounts():
                acc_id = acc["account_id"]
                if acc_id.startswith(prefix):
                    yield Completion(acc_id, start_position=start_position,
                                     display=f"{acc_id}  {acc.get('name', '')} ({acc.get('account_type', '')})")


class QMTCLI:
    def __init__(self):
        # 配置文件查找顺序: ./.env -> ~/.qmt_cli.env
        load_dotenv()
        load_dotenv(os.path.expanduser("~/.qmt_cli.env"), override=False)
        self.base_url = os.getenv("CFQUANT_BASE_URL", "http://127.0.0.1:8765")
        self.api_key = os.getenv("CFQUANT_API_KEY", None) or None
        self.account_id = os.getenv("CFQUANT_ACCOUNT_ID", "")
        self.account_type = os.getenv("CFQUANT_ACCOUNT_TYPE", "STOCK")
        
        self.client = CFQuantClient(
            base_url=self.base_url,
            api_key=self.api_key,
            account_id=self.account_id,
            account_type=self.account_type,
            timeout=30.0,
        )
        
        # 持久化历史记录
        self.history_file = ".qmt_history"
        self.session = PromptSession(history=FileHistory(self.history_file))
        self.current_sector = None

        # 定义命令和说明
        self.commands = {
            "status": "查看 cfquant 连接及账号状态",
            "accounts": "列出 cfquant 绑定账户 (含实时资产探测)",
            "use": "切换当前操作账号 (用法: use 账号前缀, 支持 Tab 补全)",
            "account": "查看账户资金资产信息",
            "pos": "查看当前持仓列表",
            "orders": "查看当日订单记录",
            "trades": "查看当日成交记录",
            "market": "获取历史 K 线 (用法: market 600519.SH [count=20] [period=1d])",
            "tick": "获取最新实时 Tick 快照 (用法: tick 600519.SH 或 tick 600519.SH,000002.SZ)",
            "download": "下载历史数据 (用法: download 600519.SH [start=20240101] [end=20240131] [period=1d])",
            "info": "查询证券基本信息 (用法: info 600519.SH)",
            "finance": "查询财务数据 (用法: finance 600519.SH)",
            "callbacks": "查看最新交易与委托回调事件 (用法: callbacks [limit=10])",
            "ls": "列出板块成分股 (用法: ls [沪深A股])",
            "buy": "下单买入 (用法: buy 600519.SH 100 [价格])",
            "sell": "下单卖出 (用法: sell 600519.SH 100 [价格])",
            "cancel": "撤单 (用法: cancel 订单ID)",
            "history": "查看历史命令",
            "clear": "清空屏幕",
            "help": "打印帮助信息",
            "exit": "退出控制台",
        }
        
        # 补全器
        self.completer = QMTCompleter(self)

    @staticmethod
    def mask_account_id(acc_id: str) -> str:
        """账号打码: 只显示前两位和后两位, 中间四个星号 (如 26****89)"""
        acc_id = str(acc_id or "")
        if len(acc_id) <= 4:
            return acc_id[0] + "****" if acc_id else ""
        return f"{acc_id[:2]}****{acc_id[-2:]}"

    def get_known_accounts(self):
        """
        获取 cfquant 绑定账户列表 (带缓存)。
        来源: 环境变量 CFQUANT_ACCOUNTS, 格式:
            CFQUANT_ACCOUNTS=账号1:STOCK:昵称1;账号2:CREDIT:昵称2
        未配置时回退到当前默认账号。
        返回: [{"account_id": str, "account_type": str, "name": str}]
        """
        now = time.time()
        cache = getattr(self, "_accounts_cache", None)
        if cache and now - cache[0] < 30:
            return cache[1]

        accounts = []
        raw = os.getenv("CFQUANT_ACCOUNTS", "").strip()
        for item in raw.split(";"):
            item = item.strip()
            if not item:
                continue
            parts = item.split(":")
            acc_id = parts[0].strip()
            if not acc_id:
                continue
            acc_type = parts[1].strip().upper() if len(parts) > 1 and parts[1].strip() else "STOCK"
            name = parts[2].strip() if len(parts) > 2 else ""
            accounts.append({
                "account_id": acc_id,
                "account_type": acc_type,
                "name": name,
            })

        if not accounts and self.account_id:
            accounts.append({
                "account_id": self.account_id,
                "account_type": self.account_type,
                "name": "(默认账号)",
            })

        self._accounts_cache = (now, accounts)
        return accounts

    def print_help(self):
        printft(HTML("\n<bold><cyan>QMT cfquant 交互控制台 (大 QMT 桥接)</cyan></bold>"))
        printft(HTML("<yellow>可用命令：</yellow>"))
        table_data = [[k, v] for k, v in self.commands.items()]
        print(tabulate(table_data, headers=["命令", "说明"], tablefmt="simple"))
        print()

    def format_table(self, data):
        if not data:
            return "无数据"
        if isinstance(data, list):
            return tabulate(data, headers="keys", tablefmt="pretty")
        elif isinstance(data, dict):
            return tabulate([data], headers="keys", tablefmt="pretty")
        return str(data)

    def run(self):
        # 初始连接测试
        # printft(HTML(f"正在连接到 cfquant 服务: <green>{self.base_url}</green> (账号: {self.account_id}) ..."))
        if self.client.ping():
            printft(HTML("<green>成功连接到 cfquant 服务!</green>"))
        else:
            printft(HTML("<yellow>无法确认 cfquant 连接，请检查网络和 API-Key。</yellow>"))

        self.print_help()

        while True:
            try:
                status_color = "green" if self.client.ping() else "red"
                sector_display = f"/{self.current_sector}" if self.current_sector else "/"
                prompt_text = HTML(f'<ansigreen>cfquant</ansigreen>:<ansiviolet>{self.account_id[-4:] if self.account_id else "None"}</ansiviolet> <ansiblue>{sector_display}</ansiblue> @ <{status_color}>{self.account_type}</{status_color}>> ')
                
                text = self.session.prompt(
                    prompt_text,
                    auto_suggest=AutoSuggestFromHistory(),
                    completer=self.completer,
                ).strip()

                if not text:
                    continue
                
                if text.lower() == "exit":
                    break
                
                self.execute(text)
                
            except KeyboardInterrupt:
                continue
            except EOFError:
                break
            except Exception as e:
                printft(HTML(f"<red>运行错误: {str(e)}</red>"))

        printft(HTML("<yellow>退出中...</yellow>"))
        self.client.close()

    def execute(self, text):
        parts = text.split()
        cmd = parts[0].lower()
        args = parts[1:]
        try:
            if cmd == "help":
                self.print_help()
            elif cmd == "clear":
                print("\033[2J\033[1;1H", end="")
            elif cmd == "status":
                res = self.client.get_asset()
                if res.get("ok"):
                    data = res.get("data", {})
                    printft(HTML(f"<green>服务正常，当前账号: {data.get('account_id', self.account_id)}, 资产: {data.get('balance', 0.0)}</green>"))
                else:
                    printft(HTML(f"<red>服务响应异常: {res}</red>"))
            elif cmd == "accounts":
                accounts = self.get_known_accounts()
                rows = []
                for acc in accounts:
                    row = {
                        "账号": self.mask_account_id(acc["account_id"]),
                        "名称": acc.get("name", ""),
                        "类型": acc.get("account_type", ""),
                    }
                    # 实时探测资产
                    probe = self.client.get_asset(acc["account_id"], acc["account_type"])
                    if probe.get("ok"):
                        data = probe.get("data", {})
                        row["状态"] = "在线"
                        row["总资产"] = data.get("balance", data.get("total_assets", ""))
                        row["可用资金"] = data.get("available", data.get("available_cash", ""))
                    else:
                        row["状态"] = "异常"
                        row["总资产"] = ""
                        row["可用资金"] = ""
                    if acc["account_id"] == self.account_id:
                        row["名称"] = f"* {row['名称']}"
                    rows.append(row)
                print(tabulate(rows, headers="keys", tablefmt="pretty"))
                printft(HTML("<dim>* 为当前操作账号</dim>"))
            elif cmd == "use":
                if not args:
                    printft(HTML("<red>缺少账号。示例: use 1234567890 或 use 1234<Tab> 补全</red>"))
                    return
                prefix = args[0].strip()
                matches = [a for a in self.get_known_accounts() if a["account_id"].startswith(prefix)]
                exact = [a for a in matches if a["account_id"] == prefix]
                target = exact[0] if exact else (matches[0] if len(matches) == 1 else None)
                if not target:
                    if matches:
                        printft(HTML(f"<yellow>前缀 {prefix} 匹配到多个账号:</yellow>"))
                        print(self.format_table([{"账号": m["account_id"], "名称": m["name"], "类型": m["account_type"]} for m in matches]))
                    else:
                        printft(HTML(f"<red>未找到以 {prefix} 开头的 cfquant 绑定账号</red>"))
                    return
                if target["account_id"] == self.account_id and target["account_type"] == self.account_type:
                    printft(HTML(f"<yellow>已在账号 {self.mask_account_id(target['account_id'])} 上</yellow>"))
                    return
                self.account_id = target["account_id"]
                self.account_type = target["account_type"]
                self.client.account_id = self.account_id
                self.client.account_type = self.account_type
                # 强制重建底层 http client 以应用新账号 (account 是 client 属性, 复用即可)
                printft(HTML(f"<green>已切换到账号 {self.mask_account_id(self.account_id)} ({target.get('name', '')}, {self.account_type})</green>"))
                res = self.client.get_asset()
                if res.get("ok"):
                    _d = res.get('data', {})
                    printft(HTML(f"<green>连接正常，总资产: {_d.get('balance', _d.get('total_assets', ''))}, 可用: {_d.get('available', _d.get('available_cash', ''))}</green>"))
                else:
                    printft(HTML(f"<yellow>切换成功但资产探测失败: {_html.escape(str(res.get('error', res)))}</yellow>"))
            elif cmd == "account":
                res = self.client.get_asset()
                if res.get("ok"):
                    print(self.format_table(res.get("data", {})))
                else:
                    printft(HTML(f"<red>查询失败: {res}</red>"))
            elif cmd == "pos":
                data = self.client.get_positions()
                print(self.format_table(data))
            elif cmd == "orders":
                data = self.client.get_orders()
                print(self.format_table(data))
            elif cmd == "trades":
                data = self.client.get_trades()
                print(self.format_table(data))
            elif cmd == "callbacks":
                limit = int(args[0]) if args else 10
                events = self.client.get_callbacks(limit=limit)
                print(self.format_table(events))
            elif cmd == "market":
                if not args:
                    printft(HTML("<red>缺少代码。示例: market 600519.SH [count=20] [period=1d]</red>"))
                    return
                symbol = args[0]
                kv = {}
                positional = []
                for a in args[1:]:
                    if "=" in a:
                        k, v = a.split("=", 1)
                        kv[k] = v
                    else:
                        positional.append(a)
                count = int(kv.get("count", positional[0] if positional else "20"))
                period = kv.get("period", positional[1] if len(positional) > 1 else "1d")
                res = self.client.get_market_data(symbol, period=period, count=count)
                norm_code = normalize_stock_code(symbol)
                data = res.get(norm_code, [])
                if data:
                    df = pd.DataFrame(data)
                    print(tabulate(df, headers="keys", tablefmt="pretty"))
                else:
                    printft(HTML("<yellow>未获取到 K 线数据。</yellow>"))
            elif cmd == "tick":
                if not args:
                    printft(HTML("<red>缺少代码。示例: tick 600519.SH 或 tick 600519.SH,000002.SZ</red>"))
                    return
                symbol = args[0]
                symbols = [s.strip() for s in symbol.split(",") if s.strip()] if "," in symbol else [symbol]
                data = self.client.get_full_tick(symbols)
                if data:
                    rows = []
                    for sym, fields in data.items():
                        row = {"代码": sym}
                        row.update(fields)
                        if "time" in row:
                            try:
                                t_val = row["time"]
                                if t_val > 1e11:
                                    row["时间"] = pd.to_datetime(t_val, unit="ms").strftime("%Y-%m-%d %H:%M:%S")
                                else:
                                    row["时间"] = pd.to_datetime(t_val, unit="s").strftime("%Y-%m-%d %H:%M:%S")
                            except:
                                pass
                        rows.append(row)
                    df = pd.DataFrame(rows)
                    display_cols = [c for c in df.columns if c not in ["bidPrice", "askPrice", "bidVol", "askVol"]]
                    print(tabulate(df[display_cols], headers="keys", tablefmt="pretty"))
                else:
                    printft(HTML("<yellow>未获取到最新 Tick 快照。</yellow>"))
            elif cmd == "download":
                if not args:
                    printft(HTML("<red>缺少代码。示例: download 600519.SH [20240101] [20240131] [1d]</red>"))
                    return
                symbol = args[0]
                start = args[1] if len(args) > 1 else ""
                end = args[2] if len(args) > 2 else ""
                period = args[3] if len(args) > 3 else "1d"
                res = self.client.download_history_data(symbol, period=period, start_time=start, end_time=end)
                print(json.dumps(res, indent=2, ensure_ascii=False))
            elif cmd == "info":
                if not args:
                    printft(HTML("<red>缺少代码。示例: info 600519.SH</red>"))
                    return
                data = self.client.get_instrument_detail(args[0])
                print(self.format_table(data))
            elif cmd == "finance":
                if not args:
                    printft(HTML("<red>缺少代码。示例: finance 600519.SH</red>"))
                    return
                data = self.client.get_financial_data(args[0])
                print(self.format_table(data))
            elif cmd == "ls":
                sector = args[0] if args else "沪深A股"
                stocks = self.client.get_sector_stocks(sector)
                if stocks:
                    printft(HTML(f"<green>板块 [{sector}] 包含 {len(stocks)} 只证券，前 20 只如下：</green>"))
                    print(", ".join(stocks[:20]))
                else:
                    printft(HTML("<yellow>未获取到板块成分股。</yellow>"))
            elif cmd in ["buy", "sell"]:
                if len(args) < 2:
                    printft(HTML(f"<red>参数不足。示例: {cmd} 600519.SH 100 [价格]</red>"))
                    return
                symbol = args[0]
                qty = float(args[1])
                price = float(args[2]) if len(args) > 2 else None
                price_type = 11 if price is not None else 50
                res = self.client.place_order(
                    stock_code=symbol,
                    side=cmd,
                    volume=qty,
                    price=price,
                    price_type=price_type,
                )
                if res.get("ok"):
                    result = res.get("data", {}).get("result", {})
                    oid = result.get("order_id", res.get("data", {}).get("order_id"))
                    printft(HTML(f"<green>下单成功, OrderID: {oid}</green>"))
                else:
                    printft(HTML(f"<red>下单失败: {res}</red>"))
            elif cmd == "cancel":
                if not args:
                    printft(HTML("<red>缺少委托编号。示例: cancel 1001</red>"))
                    return
                order_id = args[0]
                res = self.client.cancel_order(order_id)
                if res.get("ok"):
                    printft(HTML(f"<green>撤单请求成功: {order_id}</green>"))
                else:
                    printft(HTML(f"<red>撤单失败: {res}</red>"))
            elif cmd == "history":
                if os.path.exists(self.history_file):
                    with open(self.history_file, "r") as f:
                        lines = f.readlines()
                        for i, line in enumerate(lines[-50:]):
                            print(f"{i+1:3d}: {line.strip()}")
                else:
                    print("暂无历史记录")
            else:
                printft(HTML(f"<red>未知命令: {cmd}，输入 help 查看支持命令</red>"))
        except Exception as e:
            printft(HTML(f"<red>执行出错: {e}</red>"))


if __name__ == "__main__":
    cli = QMTCLI()
    if len(sys.argv) > 1:
        cmd_line = " ".join(sys.argv[1:])
        cli.execute(cmd_line)
    else:
        cli.run()
