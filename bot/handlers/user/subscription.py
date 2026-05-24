from __future__ import annotations

from datetime import datetime, timedelta

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from bot.keyboards import subscription_kb, payment_kb
from bot.states import SubscriptionSG
from config import load_config
from database.models import User, Subscription
from services.cryptobot import create_invoice

router = Router(name="subscription")
_config = load_config()


@router.callback_query(F.data == "buy_subscription")
async def cb_buy_subscription(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    result = await session.execute(
        select(User)
        .where(User.id == callback.from_user.id)
        .options(selectinload(User.subscription))
    )
    user = result.scalar_one_or_none()
    if not user:
        await callback.answer()
        return

    plans = _config.SUBSCRIPTION_PLANS
    text = "💳 <b>Купить подписку</b>\n\nВыберите тариф:"
    kb = subscription_kb()

    if user.trial_used:
        # hide trial button
        from aiogram.utils.keyboard import InlineKeyboardBuilder
        from aiogram.types import InlineKeyboardButton
        builder = InlineKeyboardBuilder()
        for plan_id, plan in plans.items():
            if plan_id == "trial":
                continue
            builder.row(InlineKeyboardButton(text=plan["label"], callback_data=f"plan_{plan_id}", style="primary"))
        builder.row(InlineKeyboardButton(text="◀ Назад", callback_data="main_menu", style="primary"))
        kb = builder.as_markup()

    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await state.set_state(SubscriptionSG.choose_plan)
    await callback.answer()


@router.callback_query(F.data.startswith("plan_"))
async def cb_choose_plan(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    plan_id = callback.data.removeprefix("plan_")
    plans = _config.SUBSCRIPTION_PLANS
    plan = plans.get(plan_id)
    if not plan:
        await callback.answer("Неизвестный тариф", show_alert=True)
        return

    result = await session.execute(
        select(User)
        .where(User.id == callback.from_user.id)
        .options(selectinload(User.subscription))
    )
    user = result.scalar_one_or_none()

    if plan_id == "trial":
        if user and user.trial_used:
            await callback.answer("Пробный период уже использован.", show_alert=True)
            return
        await _grant_subscription(session, user, plan_id, plan["days"])
        await callback.message.edit_text(
            "🎁 <b>Пробная подписка активирована!</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n\n"
            "⏳  Срок: <b>3 дня</b>\n\n"
            "Нажмите <b>«Получать запросы»</b> в главном меню — и заявки начнут поступать!",
            reply_markup=payment_kb(plan_id),
            parse_mode="HTML",
        )
        await callback.answer()
        return

    invoice_url = None
    if _config.cryptobot_token:
        try:
            invoice_url = await create_invoice(plan["price"], plan["label"], callback.from_user.id)
        except Exception:
            pass

    await state.update_data(plan_id=plan_id)
    text = (
        f"💳 <b>{plan['label']}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"💰  Стоимость: <b>{plan['price']} USDT</b>\n\n"
        "Выберите удобный способ оплаты:"
    )
    await callback.message.edit_text(
        text,
        reply_markup=payment_kb(plan_id, invoice_url),
        parse_mode="HTML",
    )
    await state.set_state(SubscriptionSG.choose_payment)
    await callback.answer()


async def _grant_subscription(
    session: AsyncSession, user: User, plan_id: str, days: int
) -> None:
    now = datetime.utcnow()
    if user.subscription and user.subscription.is_active:
        expires = user.subscription.expires_at + timedelta(days=days)
    else:
        expires = now + timedelta(days=days)

    is_paid = plan_id != "trial"
    if user.subscription:
        user.subscription.plan = plan_id
        user.subscription.expires_at = expires
        if is_paid:
            user.subscription.purchases_count += 1
    else:
        sub = Subscription(
            user_id=user.id,
            plan=plan_id,
            expires_at=expires,
            purchases_count=1 if is_paid else 0,
        )
        session.add(sub)

    if plan_id == "trial":
        user.trial_used = True

    await session.commit()
