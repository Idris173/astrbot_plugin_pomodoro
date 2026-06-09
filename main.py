import asyncio
import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Plain
from astrbot.api.star import Context, Star, register
from astrbot.core.message.message_event_result import MessageChain


@register("astrbot_plugin_pomodoro", "Idris173", "群聊多人番茄钟插件", "1.1.0")
class PomodoroPlugin(Star):
    """一个可在 QQ 群内由多人分别开启独立计时器的番茄钟插件。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        self.data_dir = Path(os.path.dirname(os.path.abspath(__file__))) / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.data_dir / "state.json"

        self.state: Dict[str, Any] = self._load_state()
        self.tasks: Dict[str, asyncio.Task] = {}
        self.state_lock = asyncio.Lock()

        logger.info("多人番茄钟插件已加载")

    async def initialize(self):
        """插件初始化。根据配置决定是否恢复已运行的个人番茄钟。"""
        if self._get_bool_config("auto_start_enabled_groups", False):
            for group_id, group_state in list(self.state.get("groups", {}).items()):
                if not group_state.get("enabled") or not group_state.get("umo"):
                    continue
                for user_id, user_state in group_state.get("users", {}).items():
                    if user_state.get("running"):
                        self._start_user_task(group_id, str(user_id))
            if self.tasks:
                logger.info(f"番茄钟已自动恢复 {len(self.tasks)} 个个人 timer")

    async def terminate(self):
        """插件卸载时取消全部后台任务。"""
        for task in list(self.tasks.values()):
            task.cancel()

        for task in list(self.tasks.values()):
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.error(f"番茄钟任务停止时出错: {e}")

        self.tasks.clear()
        self._save_state()
        logger.info("多人番茄钟插件已卸载")

    # ==================== 状态与配置 ====================

    def _load_state(self) -> Dict[str, Any]:
        default_state = {"groups": {}}
        if not self.state_path.exists():
            return default_state

        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return default_state
            data.setdefault("groups", {})
            return data
        except Exception as e:
            logger.error(f"读取番茄钟状态失败: {e}")
            return default_state

    def _save_state(self):
        try:
            with open(self.state_path, "w", encoding="utf-8") as f:
                json.dump(self.state, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存番茄钟状态失败: {e}")

    def _get_int_config(self, key: str, default: int, minimum: int = 0) -> int:
        try:
            value = int(self.config.get(key, default))
            return max(value, minimum)
        except Exception:
            return default

    def _get_bool_config(self, key: str, default: bool) -> bool:
        try:
            return bool(self.config.get(key, default))
        except Exception:
            return default

    def _get_group_id(self, event: AstrMessageEvent) -> Optional[str]:
        group_id = getattr(event.message_obj, "group_id", None)
        if group_id is None:
            return None
        group_id = str(group_id).strip()
        return group_id or None

    def _get_group_state(self, group_id: str, umo: Optional[str] = None) -> Dict[str, Any]:
        groups = self.state.setdefault("groups", {})
        group_state = groups.setdefault(
            group_id,
            {
                "umo": umo or "",
                "enabled": False,
                "completed_count": 0,
                "active_user_id": "",
                "users": {},
            },
        )
        if umo:
            group_state["umo"] = umo
        group_state.setdefault("enabled", False)
        group_state.setdefault("completed_count", 0)
        group_state.setdefault("active_user_id", "")
        group_state.setdefault("users", {})

        # 兼容旧版本 state：把旧的单群 active_user_id/completed_count 迁移为该用户的个人 timer 状态。
        old_active_user_id = str(group_state.get("active_user_id") or "").strip()
        if old_active_user_id and old_active_user_id not in group_state["users"]:
            group_state["users"][old_active_user_id] = {
                "completed_count": int(group_state.get("completed_count", 0) or 0),
                "running": False,
            }
        return group_state

    def _get_user_state(self, group_id: str, user_id: str, umo: Optional[str] = None) -> Dict[str, Any]:
        group_state = self._get_group_state(group_id, umo)
        users = group_state.setdefault("users", {})
        user_state = users.setdefault(
            str(user_id),
            {
                "completed_count": 0,
                "running": False,
            },
        )
        user_state.setdefault("completed_count", 0)
        user_state.setdefault("running", False)
        return user_state

    def _is_group_enabled(self, group_id: str) -> bool:
        return bool(self.state.get("groups", {}).get(group_id, {}).get("enabled", False))

    def _is_user_timer_enabled(self, group_id: str, user_id: str) -> bool:
        if not self._is_group_enabled(group_id):
            return False
        group_state = self.state.get("groups", {}).get(group_id, {})
        user_state = group_state.get("users", {}).get(str(user_id), {})
        return bool(user_state.get("running", False))

    async def _ensure_group_context(self, event: AstrMessageEvent) -> Optional[str]:
        group_id = self._get_group_id(event)
        if not group_id:
            return None

        async with self.state_lock:
            self._get_group_state(group_id, event.unified_msg_origin)
            self._save_state()
        return group_id

    def _get_sender_id(self, event: AstrMessageEvent) -> Optional[str]:
        """尽量从不同 AstrBot/平台事件结构中提取触发命令的 QQ 号。"""
        get_sender_id = getattr(event, "get_sender_id", None)
        if callable(get_sender_id):
            try:
                sender_id = get_sender_id()
                if sender_id:
                    return str(sender_id)
            except Exception:
                pass

        message_obj = getattr(event, "message_obj", None)
        for attr_name in ("sender_id", "user_id", "self_id"):
            sender_id = getattr(message_obj, attr_name, None)
            if sender_id:
                return str(sender_id)

        sender = getattr(message_obj, "sender", None)
        for attr_name in ("user_id", "id", "qq"):
            sender_id = getattr(sender, attr_name, None)
            if sender_id:
                return str(sender_id)

        return None

    def _reply_with_at(self, event: AstrMessageEvent, text: str):
        """回复命令时 @ 触发命令的人；无法获取发送者时退化为纯文本回复。"""
        sender_id = self._get_sender_id(event)
        if not sender_id:
            return event.plain_result(text)
        return event.chain_result([At(qq=sender_id), Plain(f"\n{text}")])

    def _timer_key(self, group_id: str, user_id: str) -> str:
        return f"{group_id}:{user_id}"

    def _get_running_user_count(self, group_id: str) -> int:
        prefix = f"{group_id}:"
        return sum(1 for key, task in self.tasks.items() if key.startswith(prefix) and not task.done())

    # ==================== 消息发送与计时任务 ====================

    async def _send_to_group(self, group_id: str, text: str, user_id: Optional[str] = None):
        group_state = self.state.get("groups", {}).get(group_id, {})
        umo = group_state.get("umo")
        if not umo:
            logger.warning(f"番茄钟群 {group_id} 缺少 unified_msg_origin，无法主动发送消息")
            return

        try:
            if user_id:
                chain = [At(qq=str(user_id)), Plain(f"\n{text}")]
            else:
                chain = [Plain(text)]
            await self.context.send_message(umo, MessageChain(chain))
        except Exception as e:
            logger.error(f"番茄钟向群 {group_id} 发送消息失败: {e}")

    def _start_user_task(self, group_id: str, user_id: str) -> bool:
        key = self._timer_key(group_id, user_id)
        old_task = self.tasks.get(key)
        if old_task and not old_task.done():
            return False

        self.tasks[key] = asyncio.create_task(self._pomodoro_loop(group_id, user_id))
        return True

    async def _stop_user_task(self, group_id: str, user_id: str):
        key = self._timer_key(group_id, user_id)
        task = self.tasks.pop(key, None)
        if not task:
            return

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"停止群 {group_id} 用户 {user_id} 番茄钟任务失败: {e}")

    async def _stop_group_tasks(self, group_id: str):
        prefix = f"{group_id}:"
        user_ids = [key.split(":", 1)[1] for key in list(self.tasks.keys()) if key.startswith(prefix)]
        for user_id in user_ids:
            await self._stop_user_task(group_id, user_id)

    async def _sleep_with_optional_reminder(self, group_id: str, user_id: str, total_minutes: int, reminder_text: str):
        total_seconds = max(total_minutes, 0) * 60
        remind_minutes = self._get_int_config("remind_before_end_minutes", 0, 0)
        remind_seconds = remind_minutes * 60

        if remind_seconds > 0 and total_seconds > remind_seconds:
            await asyncio.sleep(total_seconds - remind_seconds)
            if self._is_user_timer_enabled(group_id, user_id):
                await self._send_to_group(group_id, reminder_text, user_id)
            await asyncio.sleep(remind_seconds)
        else:
            await asyncio.sleep(total_seconds)

    async def _pomodoro_loop(self, group_id: str, user_id: str):
        """指定群内指定用户的独立番茄钟循环。"""
        try:
            while self._is_user_timer_enabled(group_id, user_id):
                work_minutes = self._get_int_config("work_minutes", 25, 1)
                short_break_minutes = self._get_int_config("short_break_minutes", 5, 1)
                long_break_minutes = self._get_int_config("long_break_minutes", 15, 1)
                long_break_every = self._get_int_config("long_break_every", 4, 1)

                user_state = self._get_user_state(group_id, user_id)
                next_count = int(user_state.get("completed_count", 0)) + 1

                await self._send_to_group(
                    group_id,
                    f"🍅 你的番茄钟开始！第 {next_count} 个番茄，专注 {work_minutes} 分钟。\n"
                    "请尽量保持专注，暂时远离摸鱼和闲聊～",
                    user_id,
                )
                await self._sleep_with_optional_reminder(
                    group_id,
                    user_id,
                    work_minutes,
                    f"⏳ 你的专注时间还剩 {self._get_int_config('remind_before_end_minutes', 0, 0)} 分钟，坚持一下！",
                )

                if not self._is_user_timer_enabled(group_id, user_id):
                    break

                async with self.state_lock:
                    user_state = self._get_user_state(group_id, user_id)
                    user_state["completed_count"] = int(user_state.get("completed_count", 0)) + 1
                    completed_count = user_state["completed_count"]
                    self._save_state()

                is_long_break = completed_count % long_break_every == 0
                break_minutes = long_break_minutes if is_long_break else short_break_minutes
                break_name = "长休息" if is_long_break else "短休息"

                await self._send_to_group(
                    group_id,
                    f"⏰ 你的第 {completed_count} 个番茄完成！现在进入 {break_minutes} 分钟{break_name}。",
                    user_id,
                )
                await self._sleep_with_optional_reminder(
                    group_id,
                    user_id,
                    break_minutes,
                    f"⏳ 你的{break_name}还剩 {self._get_int_config('remind_before_end_minutes', 0, 0)} 分钟，准备回到专注状态。",
                )

                if self._is_user_timer_enabled(group_id, user_id):
                    await self._send_to_group(group_id, "☕ 休息结束！准备进入你的下一个番茄。", user_id)

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"群 {group_id} 用户 {user_id} 番茄钟循环异常: {e}")
            await self._send_to_group(group_id, f"⚠️ 你的番茄钟任务异常停止：{e}", user_id)
        finally:
            current_task = asyncio.current_task()
            key = self._timer_key(group_id, user_id)
            if self.tasks.get(key) is current_task:
                self.tasks.pop(key, None)

    # ==================== 展示文本 ====================

    def _status_text(self, group_id: str, user_id: Optional[str]) -> str:
        group_state = self.state.get("groups", {}).get(group_id, {})
        enabled = bool(group_state.get("enabled", False))
        running_count = self._get_running_user_count(group_id)

        if user_id:
            user_state = group_state.get("users", {}).get(str(user_id), {})
            key = self._timer_key(group_id, user_id)
            user_running = key in self.tasks and not self.tasks[key].done()
            completed_count = int(user_state.get("completed_count", 0))
            user_text = (
                f"你的循环状态：{'运行中' if user_running else '未运行'}\n"
                f"你已完成番茄：{completed_count} 个\n"
            )
        else:
            user_text = "无法获取你的 QQ 号，仅显示群状态。\n"

        return (
            "🍅 当前群番茄钟状态\n"
            f"群功能状态：{'已开启' if enabled else '已关闭'}\n"
            f"群内运行 timer 数：{running_count}\n"
            f"{user_text}"
            "说明：同一群内每个人都有独立 timer。"
        )

    def _config_text(self) -> str:
        return (
            "🍅 番茄钟配置\n"
            f"专注时长：{self._get_int_config('work_minutes', 25, 1)} 分钟\n"
            f"短休息：{self._get_int_config('short_break_minutes', 5, 1)} 分钟\n"
            f"长休息：{self._get_int_config('long_break_minutes', 15, 1)} 分钟\n"
            f"长休息间隔：每 {self._get_int_config('long_break_every', 4, 1)} 个番茄\n"
            f"结束前提醒：{self._get_int_config('remind_before_end_minutes', 0, 0)} 分钟\n"
            f"启动自动恢复：{'开启' if self._get_bool_config('auto_start_enabled_groups', False) else '关闭'}"
        )

    def _help_text(self) -> str:
        return (
            "🍅 多人番茄钟插件帮助\n"
            "英文命令：\n"
            "/pomodoro on - 开启当前群番茄钟功能\n"
            "/pomodoro off - 关闭当前群番茄钟功能，并停止群内所有 timer\n"
            "/pomodoro start - 为你开始一个独立番茄钟循环\n"
            "/pomodoro stop - 停止你的番茄钟循环\n"
            "/pomodoro status - 查看当前群和你的 timer 状态\n"
            "/pomodoro config - 查看配置\n"
            "/pomodoro help - 查看帮助\n\n"
            "中文命令：\n"
            "/番茄钟 开启、/番茄钟 关闭、/番茄钟 开始、/番茄钟 停止、/番茄钟 状态、/番茄钟 配置、/番茄钟 帮助\n\n"
            "说明：同一群内不同成员可以同时开始自己的番茄钟，到时间只会 @ 对应成员。"
        )

    # ==================== 英文命令 ====================

    @filter.command("pomodoro on")
    async def pomodoro_on(self, event: AstrMessageEvent):
        group_id = await self._ensure_group_context(event)
        if not group_id:
            yield self._reply_with_at(event, "番茄钟只能在群聊中使用。")
            return

        async with self.state_lock:
            group_state = self._get_group_state(group_id, event.unified_msg_origin)
            group_state["enabled"] = True
            self._save_state()

        yield self._reply_with_at(event, "🍅 已开启当前群番茄钟功能。每个人可发送 /pomodoro start 开始自己的 timer。")

    @filter.command("pomodoro off")
    async def pomodoro_off(self, event: AstrMessageEvent):
        group_id = await self._ensure_group_context(event)
        if not group_id:
            yield self._reply_with_at(event, "番茄钟只能在群聊中使用。")
            return

        async with self.state_lock:
            group_state = self._get_group_state(group_id, event.unified_msg_origin)
            group_state["enabled"] = False
            for user_state in group_state.get("users", {}).values():
                user_state["running"] = False
            self._save_state()

        await self._stop_group_tasks(group_id)
        yield self._reply_with_at(event, "🍅 已关闭当前群番茄钟功能，并停止群内所有人的番茄钟。")

    @filter.command("pomodoro start")
    async def pomodoro_start(self, event: AstrMessageEvent):
        group_id = await self._ensure_group_context(event)
        user_id = self._get_sender_id(event)
        if not group_id:
            yield self._reply_with_at(event, "番茄钟只能在群聊中使用。")
            return
        if not user_id:
            yield event.plain_result("无法获取你的 QQ 号，不能为你创建独立番茄钟。")
            return

        async with self.state_lock:
            group_state = self._get_group_state(group_id, event.unified_msg_origin)
            group_state["enabled"] = True
            user_state = self._get_user_state(group_id, user_id, event.unified_msg_origin)
            user_state["running"] = True
            self._save_state()

        started = self._start_user_task(group_id, user_id)
        if started:
            yield self._reply_with_at(event, "🍅 你的独立番茄钟循环已开始。")
        else:
            yield self._reply_with_at(event, "🍅 你的番茄钟已经在运行中。")

    @filter.command("pomodoro stop")
    async def pomodoro_stop(self, event: AstrMessageEvent):
        group_id = await self._ensure_group_context(event)
        user_id = self._get_sender_id(event)
        if not group_id:
            yield self._reply_with_at(event, "番茄钟只能在群聊中使用。")
            return
        if not user_id:
            yield event.plain_result("无法获取你的 QQ 号，不能停止你的独立番茄钟。")
            return

        async with self.state_lock:
            user_state = self._get_user_state(group_id, user_id, event.unified_msg_origin)
            user_state["running"] = False
            self._save_state()

        await self._stop_user_task(group_id, user_id)
        yield self._reply_with_at(event, "🍅 已停止你的番茄钟循环，群内其他人的 timer 不受影响。")

    @filter.command("pomodoro status")
    async def pomodoro_status(self, event: AstrMessageEvent):
        group_id = await self._ensure_group_context(event)
        if not group_id:
            yield self._reply_with_at(event, "番茄钟只能在群聊中使用。")
            return
        yield self._reply_with_at(event, self._status_text(group_id, self._get_sender_id(event)))

    @filter.command("pomodoro config")
    async def pomodoro_config(self, event: AstrMessageEvent):
        yield self._reply_with_at(event, self._config_text())

    @filter.command("pomodoro help")
    async def pomodoro_help(self, event: AstrMessageEvent):
        yield self._reply_with_at(event, self._help_text())

    # ==================== 中文别名命令 ====================

    @filter.command("番茄钟 开启")
    async def pomodoro_on_cn(self, event: AstrMessageEvent):
        async for result in self.pomodoro_on(event):
            yield result

    @filter.command("番茄钟 关闭")
    async def pomodoro_off_cn(self, event: AstrMessageEvent):
        async for result in self.pomodoro_off(event):
            yield result

    @filter.command("番茄钟 开始")
    async def pomodoro_start_cn(self, event: AstrMessageEvent):
        async for result in self.pomodoro_start(event):
            yield result

    @filter.command("番茄钟 停止")
    async def pomodoro_stop_cn(self, event: AstrMessageEvent):
        async for result in self.pomodoro_stop(event):
            yield result

    @filter.command("番茄钟 状态")
    async def pomodoro_status_cn(self, event: AstrMessageEvent):
        async for result in self.pomodoro_status(event):
            yield result

    @filter.command("番茄钟 配置")
    async def pomodoro_config_cn(self, event: AstrMessageEvent):
        async for result in self.pomodoro_config(event):
            yield result

    @filter.command("番茄钟 帮助")
    async def pomodoro_help_cn(self, event: AstrMessageEvent):
        async for result in self.pomodoro_help(event):
            yield result