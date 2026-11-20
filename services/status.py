"""Diagnóstico de BETA HOUSE (/estado y aviso al arrancar): permisos del bot y del grupo, registros, enlaces
heredados, promoción cruzada, planes con su valor neto estimado y ajustes clave."""
from __future__ import annotations

import html
import re
from dataclasses import dataclass, field

from aiogram import Bot
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import TelegramAPIError

import config
import database as db
from services import roles


@dataclass
class Report:
    lines: list[str] = field(default_factory=list)
    warnings: int = 0
    fixable_chats: list[tuple[int, str]] = field(default_factory=list)   # grupos con permisos generales cerrados

    def ok(self, text: str) -> None:
        self.lines.append(config.ta("status_ok", text=text))

    def warn(self, text: str) -> None:
        self.warnings += 1
        self.lines.append(config.ta("status_warn", text=text))

    def section(self, key: str) -> None:
        self.lines.append(config.ta(key))

    def text(self) -> str:
        summary = (config.ta("status_summary_warn", count=self.warnings) if self.warnings
                   else config.ta("status_summary_ok"))
        return config.ta("status_title") + "\n".join(self.lines) + "\n" + summary


def _yes(value: bool) -> str:
    return "✅" if value else "❌"


def net_usd(stars: int) -> float:
    return round(stars * config.stars_usd_rate(), 2)


_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")


def price_number(text: str) -> float | None:
    """Primer número de un precio en texto («15 $ USD» → 15.0)."""
    match = _NUMBER_RE.search(text or "")
    return float(match.group().replace(",", ".")) if match else None


def stars_for_usd(usd: float) -> int:
    return int(-(-usd // config.stars_usd_rate()))   # redondeo hacia arriba


async def _chats(bot: Bot, report: Report) -> None:
    report.section("status_section_chats")
    for chat_id in sorted(config.role_chat_ids()):
        try:
            chat = await bot.get_chat(chat_id)
            me = await bot.get_chat_member(chat_id, bot.id)
        except TelegramAPIError as exc:
            report.warn(f"{chat_id}: {html.escape(exc.message)}")
            continue
        title = html.escape(chat.title or str(chat_id))
        counts = await db.count_active_by_product(chat_id)
        creators = sum(n for k, n in counts.items() if (p := config.get_product(k)) and p.member_role == "creator")
        viewers = sum(n for k, n in counts.items() if (p := config.get_product(k)) and p.member_role == "viewer")
        report.lines.append(config.ta("status_chat_line", title=title, creators=creators, viewers=viewers))
        owner = me.status == ChatMemberStatus.CREATOR
        invite = owner or bool(getattr(me, "can_invite_users", False))
        restrict = owner or bool(getattr(me, "can_restrict_members", False))
        delete = owner or bool(getattr(me, "can_delete_messages", False))
        perms = config.ta("status_bot_perms", invite=_yes(invite), restrict=_yes(restrict), delete=_yes(delete))
        (report.ok if invite and restrict and delete else report.warn)(perms)
        issues = await roles.chat_setup_issues(bot, chat_id, title)
        defaults_issue = [i for i in issues if "permisos generales" in i]
        if defaults_issue:
            for issue in defaults_issue:
                report.warn(issue)
            if restrict:
                report.fixable_chats.append((chat_id, chat.title or str(chat_id)))
        else:
            report.ok(config.ta("status_defaults_ok"))


async def _team(bot: Bot, report: Report) -> None:
    report.section("status_section_team")
    try:
        chat = await bot.get_chat(config.ADMIN_GROUP_ID)
        me = await bot.get_chat_member(config.ADMIN_GROUP_ID, bot.id)
        topics_ok = bool(chat.is_forum) and (
            me.status == ChatMemberStatus.CREATOR or bool(getattr(me, "can_manage_topics", False)))
    except TelegramAPIError:
        topics_ok = False
    (report.ok if topics_ok else report.warn)(
        config.ta("status_admin_group_ok" if topics_ok else "status_admin_group_bad"))
    logs = [(config.SALES_LOG_CHANNEL_ID, config.ta("status_log_sales"))]
    if config.TIPS_ENABLED:
        logs.append((config.TIPS_LOG_CHANNEL_ID, config.ta("status_log_tips")))
    for chat_id, name in logs:
        try:
            chat = await bot.get_chat(chat_id)
            me = await bot.get_chat_member(chat_id, bot.id)
            can = me.status == ChatMemberStatus.CREATOR or (
                me.status == ChatMemberStatus.ADMINISTRATOR
                and (chat.type != ChatType.CHANNEL or bool(getattr(me, "can_post_messages", False))))
        except TelegramAPIError:
            can = False
        (report.ok if can else report.warn)(config.ta("status_log_ok" if can else "status_log_bad", name=name))


def _links(report: Report) -> None:
    report.section("status_section_links")
    clean = True
    for key, (_cat, label, _default, optional) in config.LINK_CATALOG.items():
        value = config.link(key)
        if value in config.FACTORY_LINK_VALUES and not config.setting_is_custom(key):
            report.warn(config.ta("status_link_factory", label=html.escape(label), value=html.escape(value)))
            clean = False
        elif not value and key in ("rules_link",):
            report.lines.append("ℹ️ " + config.ta("status_link_empty", label=html.escape(label)))
    if clean:
        report.ok(config.ta("status_links_ok"))
    promo = config.setting_value(f"cross_promo_{config.DEFAULT_LANG}")
    report.lines.append("ℹ️ " + config.ta("status_cross_promo", value=html.escape(promo)))


def _plans(report: Report) -> None:
    report.section("status_section_plans")
    for product in config.all_products():
        if product.member_role not in ("creator", "viewer"):
            continue
        active = config.plans(product.key)
        if not active:
            report.warn(config.ta("status_no_plans", product=html.escape(product.name_es)))
            continue
        for plan in active:
            report.lines.append("• " + config.ta(
                "status_plan_line", product=html.escape(product.name_es), plan=config.plan_label_admin(plan.months),
                stars=plan.stars_price or "—", usd=f"{net_usd(plan.stars_price):.2f}" if plan.stars_price else "—",
                ext=html.escape(plan.ext_price) if plan.ext_price else "—",
            ))


def _settings(report: Report) -> None:
    report.section("status_section_settings")
    report.lines.append(config.ta(
        "status_settings_line", mode=config.ta(f"access_mode_{config.access_mode()}"),
        viewer=config.ta(f"viewer_mode_{config.viewer_mode()}"),
        video=config.ta("state_on" if config.videochat_alert_enabled() else "state_off"),
    ))


async def build(bot: Bot) -> Report:
    report = Report()
    await _chats(bot, report)
    await _team(bot, report)
    _links(report)
    _plans(report)
    _settings(report)
    return report
