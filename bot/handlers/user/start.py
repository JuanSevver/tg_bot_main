from __future__ import annotations

import random
from datetime import datetime, timedelta

from aiogram import Router, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from bot.states import CaptchaSG, UserSG
from bot.keyboards import main_menu_kb
from config import load_config
from database.models import User, Subscription, Category, UserCategory

router = Router(name="start")
_config = load_config()

WELCOME_TEXT = (
    "Добро пожаловать!\n\n"
    "Рад приветствовать вас в боте!\n\n"
    "📍 Вся необходимая информация находится в разделах «Поддержка» или «Инструкция».\n\n"
    "Также вы можете зайти в «Категории» и выбрать интересующие вас темы.\n\n"
    "Приятного использования!"
)

CAPTCHA_TEXT = (
    "🤖 <b>Проверка</b>\n"
    "━━━━━━━━━━━━━━━━━━━━━\n\n"
    "Чтобы убедиться, что вы не робот,\n"
    "решите простой пример:\n\n"
    "🔢  <b>{expr} = ?</b>\n\n"
    "✏️ Введите ответ:"
)


def _generate_captcha() -> tuple[str, int]:
    a, b = random.randint(1, 10), random.randint(1, 10)
    return f"{a} + {b}", a + b


async def _get_or_create_user(session: AsyncSession, tg_user) -> User:
    result = await session.execute(
        select(User)
        .where(User.id == tg_user.id)
        .options(selectinload(User.subscription), selectinload(User.categories))
    )
    user = result.scalar_one_or_none()
    if not user:
        user = User(
            id=tg_user.id,
            username=tg_user.username,
            full_name=tg_user.full_name or tg_user.first_name or "",
        )
        session.add(user)
        await session.commit()
        await session.refresh(user, ["subscription", "categories"])
    return user


async def _send_main_menu(message: Message | CallbackQuery, user: User) -> None:
    text = (
        "🏠 <b>Главное меню</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Добро пожаловать!\n\n"
        "Рад приветствовать вас в боте!\n\n"
        "📍 Вся необходимая информация находится в разделах «Поддержка» или «Инструкция».\n\n"
        "Также вы можете зайти в «Категории» и выбрать интересующие вас темы.\n\n"
        "Приятного использования!"
    )
    kb = main_menu_kb(user.receiving_enabled)
    if isinstance(message, Message):
        await message.answer(text, reply_markup=kb, parse_mode="HTML")
    else:
        await message.message.edit_text(text, reply_markup=kb, parse_mode="HTML")


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, session: AsyncSession) -> None:
    user = await _get_or_create_user(session, message.from_user)

    if user.is_blocked:
        await message.answer("🚫 Ваш аккаунт заблокирован. Обратитесь в поддержку.")
        return

    is_new = not user.captcha_passed

    if is_new:
        await message.answer(WELCOME_TEXT, parse_mode="HTML")
        expr, answer = _generate_captcha()
        await state.set_state(CaptchaSG.waiting_answer)
        await state.update_data(captcha_answer=answer, user_id=user.id)
        await message.answer(CAPTCHA_TEXT.format(expr=expr), parse_mode="HTML")
        return

    await state.set_state(UserSG.main_menu)
    await _send_main_menu(message, user)


@router.message(CaptchaSG.waiting_answer)
async def process_captcha(message: Message, state: FSMContext, session: AsyncSession) -> None:
    data = await state.get_data()
    correct: int = data.get("captcha_answer", -1)
    user_id: int = data.get("user_id")

    try:
        given = int(message.text.strip())
    except (ValueError, AttributeError):
        await message.answer("❌ Введите число — ответ на пример.")
        return

    if given != correct:
        expr, answer = _generate_captcha()
        await state.update_data(captcha_answer=answer)
        await message.answer(
            f"❌ <b>Неверно!</b> Попробуйте ещё раз:\n\n🔢  <b>{expr} = ?</b>",
            parse_mode="HTML",
        )
        return

    result = await session.execute(
        select(User)
        .where(User.id == user_id)
        .options(selectinload(User.subscription), selectinload(User.categories))
    )
    user = result.scalar_one_or_none()
    if not user:
        await message.answer("❌ Ошибка: пользователь не найден. Нажмите /start заново.")
        return
    user.captcha_passed = True
    user.receiving_enabled = True

    if not user.trial_used and not user.subscription:
        expires = datetime.utcnow() + timedelta(days=3)
        sub = Subscription(user_id=user.id, plan="trial", expires_at=expires, purchases_count=0)
        session.add(sub)
        user.trial_used = True

    cats_result = await session.execute(select(Category).where(Category.is_active == True))
    all_cats = cats_result.scalars().all()
    existing_cat_ids = {uc.category_id for uc in user.categories}
    for cat in all_cats:
        if cat.id not in existing_cat_ids:
            uc = UserCategory(user_id=user.id, category_id=cat.id, enabled=True)
            session.add(uc)

    await session.commit()
    await session.refresh(user, ["subscription", "categories"])

    await message.answer(
        "✅ <b>Проверка пройдена!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        "🎁 Вам активирована <b>пробная подписка на 3 дня</b>.\n\n"
        "Нажмите <b>«Получать запросы»</b> в меню — и заявки начнут поступать!",
        parse_mode="HTML",
    )
    await state.set_state(UserSG.main_menu)
    await _send_main_menu(message, user)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    from bot.keyboards import instruction_kb
    text = (
        "📋 <b>Инструкция</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        "1️⃣  Нажмите <b>«Получать запросы»</b> — лента включится.\n\n"
        "2️⃣  Зайдите в <b>«Категории»</b> и выберите нужные тематики.\n"
        "     Остальные можно отключить — лишнего не придёт.\n\n"
        "3️⃣  Заявки будут приходить <b>прямо сюда</b>, без дублей.\n\n"
        "4️⃣  Под каждым сообщением кнопка\n"
        "     <b>«Написать пользователю»</b> — один тап и вы в диалоге.\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "💡 Подписку можно продлить в разделе <b>«Купить подписку»</b>."
    )
    await message.answer(text, reply_markup=instruction_kb(), parse_mode="HTML")


@router.message(Command("profile"))
async def cmd_profile(message: Message, state: FSMContext, session: AsyncSession) -> None:
    from bot.handlers.user.profile import cb_profile
    # Переадресуем в главное меню с имитацией callback
    result = await session.execute(
        select(User).where(User.id == message.from_user.id).options(selectinload(User.subscription))
    )
    user = result.scalar_one_or_none()
    if not user:
        await message.answer("Сначала нажмите /start")
        return
    from bot.keyboards import profile_kb
    from database.models import User as UserModel
    sub = user.subscription
    if sub and sub.is_active:
        from bot.handlers.user.profile import PLAN_LABELS
        plan_label = PLAN_LABELS.get(sub.plan, sub.plan)
        sub_block = (
            f"  ├ Статус: <b>Активна ✅</b>\n"
            f"  ├ Тариф: <b>{plan_label}</b>\n"
            f"  └ Осталось: <b>{sub.days_left} дн.</b>"
        )
    else:
        sub_block = "  └ <i>Нет активной подписки</i>"
    receiving = "Включена 🟢" if user.receiving_enabled else "Выключена 🔴"
    text = (
        "👤 <b>Профиль</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🆔  ID: <code>{user.id}</code>\n"
        f"📨  Получено заявок: <b>{user.messages_received}</b>\n"
        f"📡  Лента запросов: <b>{receiving}</b>\n\n"
        "💳  <b>Подписка:</b>\n"
        f"{sub_block}"
    )
    await message.answer(text, reply_markup=profile_kb(), parse_mode="HTML")


@router.message(Command("categories"))
async def cmd_categories(message: Message, state: FSMContext, session: AsyncSession) -> None:
    result = await session.execute(
        select(User).where(User.id == message.from_user.id)
    )
    user = result.scalar_one_or_none()
    if not user:
        await message.answer("Сначала нажмите /start")
        return
    await state.set_state(UserSG.main_menu)
    await _send_main_menu(message, user)


@router.message(Command("subscribe"))
async def cmd_subscribe(message: Message, state: FSMContext, session: AsyncSession) -> None:
    result = await session.execute(
        select(User).where(User.id == message.from_user.id).options(selectinload(User.subscription))
    )
    user = result.scalar_one_or_none()
    if not user:
        await message.answer("Сначала нажмите /start")
        return
    from bot.keyboards import subscription_kb
    await message.answer(
        "💳 <b>Купить подписку</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Выберите подходящий тариф:",
        reply_markup=subscription_kb(),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "main_menu")
async def cb_main_menu(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    result = await session.execute(
        select(User)
        .where(User.id == callback.from_user.id)
        .options(selectinload(User.subscription))
    )
    user = result.scalar_one_or_none()
    if not user:
        await callback.answer()
        return
    await state.set_state(UserSG.main_menu)
    await _send_main_menu(callback, user)
    await callback.answer()


@router.callback_query(F.data == "toggle_receive")
async def cb_toggle_receive(callback: CallbackQuery, session: AsyncSession) -> None:
    result = await session.execute(
        select(User)
        .where(User.id == callback.from_user.id)
        .options(selectinload(User.subscription))
    )
    user = result.scalar_one_or_none()
    if not user:
        await callback.answer()
        return

    sub = user.subscription
    if not sub or not sub.is_active:
        await callback.answer("❌ У вас нет активной подписки!", show_alert=True)
        return

    user.receiving_enabled = not user.receiving_enabled
    await session.commit()

    status = "включена ✅" if user.receiving_enabled else "отключена 🔴"
    await callback.answer(f"Лента запросов {status}", show_alert=True)
    await callback.message.edit_reply_markup(reply_markup=main_menu_kb(user.receiving_enabled))


@router.callback_query(F.data == "instruction")
async def cb_instruction(callback: CallbackQuery) -> None:
    from bot.keyboards import instruction_kb
    text = (
        "📋 <b>Инструкция</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        "1️⃣  Нажмите <b>«Получать запросы»</b> — лента включится.\n\n"
        "2️⃣  Зайдите в <b>«Категории»</b> и выберите нужные тематики.\n"
        "     Остальные можно отключить — лишнего не придёт.\n\n"
        "3️⃣  Заявки будут приходить <b>прямо сюда</b>, без дублей.\n\n"
        "4️⃣  Под каждым сообщением кнопка\n"
        "     <b>«Написать пользователю»</b> — один тап и вы в диалоге.\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "💡 Подписку можно продлить в разделе <b>«Купить подписку»</b>."
    )
    await callback.message.edit_text(text, reply_markup=instruction_kb(), parse_mode="HTML")
    await callback.answer()
