import logging
from typing import Optional, Dict, Any, List
from datetime import datetime
from aiogram import Bot, types
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.promo_group import get_promo_group_by_id
from app.database.crud.user import get_user_by_id
from app.database.models import (
    AdvertisingCampaign,
    PromoCodeType,
    PromoGroup,
    Subscription,
    Transaction,
    TransactionType,
    User,
)

logger = logging.getLogger(__name__)


class AdminNotificationService:
    
    def __init__(self, bot: Bot):
        self.bot = bot
        self.chat_id = getattr(settings, 'ADMIN_NOTIFICATIONS_CHAT_ID', None)
        self.topic_id = getattr(settings, 'ADMIN_NOTIFICATIONS_TOPIC_ID', None)
        self.ticket_topic_id = getattr(settings, 'ADMIN_NOTIFICATIONS_TICKET_TOPIC_ID', None)
        self.enabled = getattr(settings, 'ADMIN_NOTIFICATIONS_ENABLED', False)
    
    async def _get_referrer_info(self, db: AsyncSession, referred_by_id: Optional[int]) -> str:
        if not referred_by_id:
            return "Нет"

        try:
            referrer = await get_user_by_id(db, referred_by_id)
            if not referrer:
                return f"ID {referred_by_id} (не найден)"

            if referrer.username:
                return f"@{referrer.username} (ID: {referred_by_id})"
            else:
                return f"ID {referrer.telegram_id}"

        except Exception as e:
            logger.error(f"Ошибка получения данных рефера {referred_by_id}: {e}")
            return f"ID {referred_by_id}"

    async def _get_user_promo_group(self, db: AsyncSession, user: User) -> Optional[PromoGroup]:
        if "promo_group" in user.__dict__:
            return user.__dict__["promo_group"]

        if not user.promo_group_id:
            return None

        try:
            await db.refresh(user, attribute_names=["promo_group"])
        except Exception:
            # Refresh may fail if relationship is not configured; fallback to manual fetch.
            pass
        else:
            if "promo_group" in user.__dict__:
                return user.__dict__["promo_group"]

        try:
            return await get_promo_group_by_id(db, user.promo_group_id)
        except Exception as e:
            logger.error(
                "Failed to load promo group %s for user %s: %s",
                user.promo_group_id,
                user.telegram_id,
                e,
            )
            return None
    def _format_promo_group_discounts(self, promo_group: PromoGroup) -> List[str]:
        discount_lines: List[str] = []

        discount_map = {
            "servers": ("Серверы", promo_group.server_discount_percent),
            "traffic": ("Трафик", promo_group.traffic_discount_percent),
            "devices": ("Устройства", promo_group.device_discount_percent),
        }

        for _, (title, percent) in discount_map.items():
            if percent and percent > 0:
                discount_lines.append(f"• {title}: -{percent}%")

        period_discounts_raw = promo_group.period_discounts or {}
        period_items: List[tuple[int, int]] = []

        if isinstance(period_discounts_raw, dict):
            for raw_days, raw_percent in period_discounts_raw.items():
                try:
                    days = int(raw_days)
                    percent = int(raw_percent)
                except (TypeError, ValueError):
                    continue

                if percent > 0:
                    period_items.append((days, percent))

        period_items.sort(key=lambda item: item[0])

        if period_items:
            formatted_periods = ", ".join(
                f"{days} д. — -{percent}%" for days, percent in period_items
            )
            discount_lines.append(f"• Периоды: {formatted_periods}")

        if promo_group.apply_discounts_to_addons:
            discount_lines.append("• Доп. услуги: ✅ скидка действует")
        else:
            discount_lines.append("• Доп. услуги: ❌ без скидки")

        return discount_lines

    def _format_promo_group_block(
        self,
        promo_group: Optional[PromoGroup],
        *,
        title: str = "Промогруппа",
        icon: str = "🏷️",
    ) -> str:
        if not promo_group:
            return f"{icon} <b>{title}:</b> —"

        lines = [f"{icon} <b>{title}:</b> {promo_group.name}"]

        discount_lines = self._format_promo_group_discounts(promo_group)
        if discount_lines:
            lines.append("💸 <b>Скидки:</b>")
            lines.extend(discount_lines)
        else:
            lines.append("💸 <b>Скидки:</b> отсутствуют")

        return "\n".join(lines)

    def _get_promocode_type_display(self, promo_type: Optional[str]) -> str:
        mapping = {
            PromoCodeType.BALANCE.value: "💰 Бонус на баланс",
            PromoCodeType.SUBSCRIPTION_DAYS.value: "⏰ Доп. дни подписки",
            PromoCodeType.TRIAL_SUBSCRIPTION.value: "🎁 Триал подписка",
        }

        if not promo_type:
            return "ℹ️ Не указан"

        return mapping.get(promo_type, f"ℹ️ {promo_type}")

    def _format_campaign_bonus(self, campaign: AdvertisingCampaign) -> List[str]:
        if campaign.is_balance_bonus:
            return [
                f"💰 Баланс: {settings.format_price(campaign.balance_bonus_kopeks or 0)}",
            ]

        if campaign.is_subscription_bonus:
            default_devices = getattr(settings, "DEFAULT_DEVICE_LIMIT", 1)
            details = [
                f"📅 Дней подписки: {campaign.subscription_duration_days or 0}",
                f"📊 Трафик: {campaign.subscription_traffic_gb or 0} ГБ",
                f"📱 Устройства: {campaign.subscription_device_limit or default_devices}",
            ]
            if campaign.subscription_squads:
                details.append(f"🌐 Сквады: {len(campaign.subscription_squads)} шт.")
            return details

        return ["ℹ️ Бонусы не предусмотрены"]
    
    async def send_trial_activation_notification(
        self,
        db: AsyncSession,
        user: User,
        subscription: Subscription
    ) -> bool:
        if not self._is_enabled():
            return False
        
        try:
            user_status = "🆕 Новый" if not user.has_had_paid_subscription else "🔄 Существующий"
            referrer_info = await self._get_referrer_info(db, user.referred_by_id)
            promo_group = await self._get_user_promo_group(db, user)
            promo_block = self._format_promo_group_block(promo_group)

            message = f"""🎯 <b>АКТИВАЦИЯ ТРИАЛА</b>

👤 <b>Пользователь:</b> {user.full_name}
🆔 <b>Telegram ID:</b> <code>{user.telegram_id}</code>
📱 <b>Username:</b> @{user.username or 'отсутствует'}
👥 <b>Статус:</b> {user_status}

{promo_block}

⏰ <b>Параметры триала:</b>
📅 Период: {settings.TRIAL_DURATION_DAYS} дней
📊 Трафик: {settings.TRIAL_TRAFFIC_LIMIT_GB} ГБ
📱 Устройства: {settings.TRIAL_DEVICE_LIMIT}
🌐 Сервер: {subscription.connected_squads[0] if subscription.connected_squads else 'По умолчанию'}

📆 <b>Действует до:</b> {subscription.end_date.strftime('%d.%m.%Y %H:%M')}
🔗 <b>Реферер:</b> {referrer_info}

⏰ <i>{datetime.now().strftime('%d.%m.%Y %H:%M:%S')}</i>"""
            
            return await self._send_message(message)
            
        except Exception as e:
            logger.error(f"Ошибка отправки уведомления о триале: {e}")
            return False
    
    async def send_subscription_purchase_notification(
        self,
        db: AsyncSession,
        user: User,
        subscription: Subscription,
        transaction: Transaction,
        period_days: int,
        was_trial_conversion: bool = False
    ) -> bool:
        if not self._is_enabled():
            return False
        
        try:
            event_type = "🔄 КОНВЕРСИЯ ИЗ ТРИАЛА" if was_trial_conversion else "💎 ПОКУПКА ПОДПИСКИ"
            
            if was_trial_conversion:
                user_status = "🎯 Конверсия из триала"
            elif user.has_had_paid_subscription:
                user_status = "🔄 Продление/Обновление"
            else:
                user_status = "🆕 Первая покупка"
            
            servers_info = await self._get_servers_info(subscription.connected_squads)
            payment_method = self._get_payment_method_display(transaction.payment_method)
            referrer_info = await self._get_referrer_info(db, user.referred_by_id)
            promo_group = await self._get_user_promo_group(db, user)
            promo_block = self._format_promo_group_block(promo_group)

            message = f"""💎 <b>{event_type}</b>

👤 <b>Пользователь:</b> {user.full_name}
🆔 <b>Telegram ID:</b> <code>{user.telegram_id}</code>
📱 <b>Username:</b> @{user.username or 'отсутствует'}
👥 <b>Статус:</b> {user_status}

{promo_block}

💰 <b>Платеж:</b>
💵 Сумма: {settings.format_price(transaction.amount_kopeks)}
💳 Способ: {payment_method}
🆔 ID транзакции: {transaction.id}

📱 <b>Параметры подписки:</b>
📅 Период: {period_days} дней
📊 Трафик: {self._format_traffic(subscription.traffic_limit_gb)}
📱 Устройства: {subscription.device_limit}
🌐 Серверы: {servers_info}

📆 <b>Действует до:</b> {subscription.end_date.strftime('%d.%m.%Y %H:%M')}
💰 <b>Баланс после покупки:</b> {settings.format_price(user.balance_kopeks)}
🔗 <b>Реферер:</b> {referrer_info}

⏰ <i>{datetime.now().strftime('%d.%m.%Y %H:%M:%S')}</i>"""
            
            return await self._send_message(message)
            
        except Exception as e:
            logger.error(f"Ошибка отправки уведомления о покупке: {e}")
            return False

    async def send_version_update_notification(
        self,
        current_version: str,
        latest_version, 
        total_updates: int
    ) -> bool:
        """Отправляет уведомление о новых обновлениях"""
        if not self._is_enabled():
            return False
        
        try:
            if latest_version.prerelease:
                update_type = "🧪 ПРЕДВАРИТЕЛЬНАЯ ВЕРСИЯ"
                type_icon = "🧪"
            elif latest_version.is_dev:
                update_type = "🔧 DEV ВЕРСИЯ"
                type_icon = "🔧"
            else:
                update_type = "📦 НОВАЯ ВЕРСИЯ"
                type_icon = "📦"
            
            description = latest_version.short_description
            if len(description) > 200:
                description = description[:197] + "..."
            
            message = f"""{type_icon} <b>{update_type} ДОСТУПНА</b>
    
    📦 <b>Текущая версия:</b> <code>{current_version}</code>
    🆕 <b>Новая версия:</b> <code>{latest_version.tag_name}</code>
    📅 <b>Дата релиза:</b> {latest_version.formatted_date}
    
    📝 <b>Описание:</b>
    {description}
    
    🔢 <b>Всего доступно обновлений:</b> {total_updates}
    🔗 <b>Репозиторий:</b> https://github.com/{getattr(self, 'repo', 'fr1ngg/remnawave-bedolaga-telegram-bot')}
    
    ℹ️ Для обновления перезапустите контейнер с новым тегом или обновите код из репозитория.
    
    ⚙️ <i>Автоматическая проверка обновлений • {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}</i>"""
            
            return await self._send_message(message)
            
        except Exception as e:
            logger.error(f"Ошибка отправки уведомления об обновлении: {e}")
            return False
    
    async def send_version_check_error_notification(
        self,
        error_message: str,
        current_version: str
    ) -> bool:
        if not self._is_enabled():
            return False
        
        try:
            message = f"""⚠️ <b>ОШИБКА ПРОВЕРКИ ОБНОВЛЕНИЙ</b>
    
    📦 <b>Текущая версия:</b> <code>{current_version}</code>
    ❌ <b>Ошибка:</b> {error_message}
    
    🔄 Следующая попытка через час.
    ⚙️ Проверьте доступность GitHub API и настройки сети.
    
    ⚙️ <i>Система автоматических обновлений • {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}</i>"""
            
            return await self._send_message(message)
            
        except Exception as e:
            logger.error(f"Ошибка отправки уведомления об ошибке проверки версий: {e}")
            return False
    
    async def send_balance_topup_notification(
        self,
        db: AsyncSession,
        user: User,
        transaction: Transaction,
        old_balance: int
    ) -> bool:
        if not self._is_enabled():
            return False

        try:
            deposit_count_result = await db.execute(
                select(func.count())
                .select_from(Transaction)
                .where(
                    Transaction.user_id == user.id,
                    Transaction.type == TransactionType.DEPOSIT.value,
                    Transaction.is_completed.is_(True)
                )
            )
            deposit_count = deposit_count_result.scalar_one() or 0
            topup_status = "🆕 Первое пополнение" if deposit_count <= 1 else "🔄 Пополнение"
            payment_method = self._get_payment_method_display(transaction.payment_method)
            balance_change = user.balance_kopeks - old_balance
            referrer_info = await self._get_referrer_info(db, user.referred_by_id)
            subscription_result = await db.execute(
                select(Subscription).where(Subscription.user_id == user.id)
            )
            subscription = subscription_result.scalar_one_or_none()
            subscription_status = self._get_subscription_status(subscription)
            promo_group = await self._get_user_promo_group(db, user)
            promo_block = self._format_promo_group_block(promo_group)

            message = f"""💰 <b>ПОПОЛНЕНИЕ БАЛАНСА</b>

👤 <b>Пользователь:</b> {user.full_name}
🆔 <b>Telegram ID:</b> <code>{user.telegram_id}</code>
📱 <b>Username:</b> @{user.username or 'отсутствует'}
💳 <b>Статус:</b> {topup_status}

{promo_block}

💰 <b>Детали пополнения:</b>
💵 Сумма: {settings.format_price(transaction.amount_kopeks)}
💳 Способ: {payment_method}
🆔 ID транзакции: {transaction.id}

💰 <b>Баланс:</b>
📉 Было: {settings.format_price(old_balance)}
📈 Стало: {settings.format_price(user.balance_kopeks)}
➕ Изменение: +{settings.format_price(balance_change)}

🔗 <b>Реферер:</b> {referrer_info}
📱 <b>Подписка:</b> {subscription_status}

⏰ <i>{datetime.now().strftime('%d.%m.%Y %H:%M:%S')}</i>"""
            
            return await self._send_message(message)
            
        except Exception as e:
            logger.error(f"Ошибка отправки уведомления о пополнении: {e}")
            return False
    
    async def send_subscription_extension_notification(
        self,
        db: AsyncSession,
        user: User,
        subscription: Subscription,
        transaction: Transaction,
        extended_days: int,
        old_end_date: datetime,
        *,
        new_end_date: datetime | None = None,
        balance_after: int | None = None,
    ) -> bool:
        if not self._is_enabled():
            return False

        try:
            payment_method = self._get_payment_method_display(transaction.payment_method)
            servers_info = await self._get_servers_info(subscription.connected_squads)
            promo_group = await self._get_user_promo_group(db, user)
            promo_block = self._format_promo_group_block(promo_group)

            current_end_date = new_end_date or subscription.end_date
            current_balance = balance_after if balance_after is not None else user.balance_kopeks

            message = f"""⏰ <b>ПРОДЛЕНИЕ ПОДПИСКИ</b>

👤 <b>Пользователь:</b> {user.full_name}
🆔 <b>Telegram ID:</b> <code>{user.telegram_id}</code>
📱 <b>Username:</b> @{user.username or 'отсутствует'}

{promo_block}

💰 <b>Платеж:</b>
💵 Сумма: {settings.format_price(transaction.amount_kopeks)}
💳 Способ: {payment_method}
🆔 ID транзакции: {transaction.id}

📅 <b>Продление:</b>
➕ Добавлено дней: {extended_days}
📆 Было до: {old_end_date.strftime('%d.%m.%Y %H:%M')}
📆 Стало до: {current_end_date.strftime('%d.%m.%Y %H:%M')}

📱 <b>Текущие параметры:</b>
📊 Трафик: {self._format_traffic(subscription.traffic_limit_gb)}
📱 Устройства: {subscription.device_limit}
🌐 Серверы: {servers_info}

💰 <b>Баланс после операции:</b> {settings.format_price(current_balance)}

⏰ <i>{datetime.now().strftime('%d.%m.%Y %H:%M:%S')}</i>"""

            return await self._send_message(message)

        except Exception as e:
            logger.error(f"Ошибка отправки уведомления о продлении: {e}")
            return False

    async def send_promocode_activation_notification(
        self,
        db: AsyncSession,
        user: User,
        promocode_data: Dict[str, Any],
        effect_description: str,
    ) -> bool:
        if not self._is_enabled():
            return False

        try:
            promo_group = await self._get_user_promo_group(db, user)
            promo_block = self._format_promo_group_block(promo_group)
            type_display = self._get_promocode_type_display(promocode_data.get("type"))
            usage_info = f"{promocode_data.get('current_uses', 0)}/{promocode_data.get('max_uses', 0)}"

            message_lines = [
                "🎫 <b>АКТИВАЦИЯ ПРОМОКОДА</b>",
                "",
                f"👤 <b>Пользователь:</b> {user.full_name}",
                f"🆔 <b>Telegram ID:</b> <code>{user.telegram_id}</code>",
                f"📱 <b>Username:</b> @{user.username or 'отсутствует'}",
                "",
                promo_block,
                "",
                "🎟️ <b>Промокод:</b>",
                f"🔖 Код: <code>{promocode_data.get('code')}</code>",
                f"🧾 Тип: {type_display}",
                f"📊 Использования: {usage_info}",
            ]

            balance_bonus = promocode_data.get("balance_bonus_kopeks", 0)
            if balance_bonus:
                message_lines.append(
                    f"💰 Бонус на баланс: {settings.format_price(balance_bonus)}"
                )

            subscription_days = promocode_data.get("subscription_days", 0)
            if subscription_days:
                message_lines.append(f"📅 Доп. дни подписки: {subscription_days}")

            valid_until = promocode_data.get("valid_until")
            if valid_until:
                message_lines.append(
                    f"⏳ Действует до: {valid_until.strftime('%d.%m.%Y %H:%M')}"
                    if isinstance(valid_until, datetime)
                    else f"⏳ Действует до: {valid_until}"
                )

            message_lines.extend(
                [
                    "",
                    "📝 <b>Эффект:</b>",
                    effect_description.strip() or "✅ Промокод активирован",
                    "",
                    f"⏰ <i>{datetime.now().strftime('%d.%m.%Y %H:%M:%S')}</i>",
                ]
            )

            return await self._send_message("\n".join(message_lines))

        except Exception as e:
            logger.error(f"Ошибка отправки уведомления об активации промокода: {e}")
            return False

    async def send_campaign_link_visit_notification(
        self,
        db: AsyncSession,
        telegram_user: types.User,
        campaign: AdvertisingCampaign,
        user: Optional[User] = None,
    ) -> bool:
        if not self._is_enabled():
            return False

        try:
            user_status = "🆕 Новый пользователь" if not user else "👥 Уже зарегистрирован"
            promo_block = (
                self._format_promo_group_block(await self._get_user_promo_group(db, user))
                if user
                else self._format_promo_group_block(None)
            )

            full_name = telegram_user.full_name or telegram_user.username or str(telegram_user.id)
            username = f"@{telegram_user.username}" if telegram_user.username else "отсутствует"

            message_lines = [
                "📣 <b>ПЕРЕХОД ПО РЕКЛАМНОЙ КАМПАНИИ</b>",
                "",
                f"🧾 <b>Кампания:</b> {campaign.name}",
                f"🆔 ID кампании: {campaign.id}",
                f"🔗 Start-параметр: <code>{campaign.start_parameter}</code>",
                "",
                f"👤 <b>Пользователь:</b> {full_name}",
                f"🆔 <b>Telegram ID:</b> <code>{telegram_user.id}</code>",
                f"📱 <b>Username:</b> {username}",
                user_status,
                "",
                promo_block,
                "",
                "🎯 <b>Бонус кампании:</b>",
            ]

            bonus_lines = self._format_campaign_bonus(campaign)
            message_lines.extend(bonus_lines)

            message_lines.extend(
                [
                    "",
                    f"⏰ <i>{datetime.now().strftime('%d.%m.%Y %H:%M:%S')}</i>",
                ]
            )

            return await self._send_message("\n".join(message_lines))

        except Exception as e:
            logger.error(f"Ошибка отправки уведомления о переходе по кампании: {e}")
            return False

    async def send_user_promo_group_change_notification(
        self,
        db: AsyncSession,
        user: User,
        old_group: Optional[PromoGroup],
        new_group: PromoGroup,
        *,
        reason: Optional[str] = None,
        initiator: Optional[User] = None,
        automatic: bool = False,
    ) -> bool:
        if not self._is_enabled():
            return False

        try:
            title = "🤖 АВТОМАТИЧЕСКАЯ СМЕНА ПРОМОГРУППЫ" if automatic else "👥 СМЕНА ПРОМОГРУППЫ"
            initiator_line = None
            if initiator:
                initiator_line = (
                    f"👮 <b>Инициатор:</b> {initiator.full_name} (ID: {initiator.telegram_id})"
                )
            elif automatic:
                initiator_line = "🤖 Автоматическое назначение"

            message_lines = [
                f"{title}",
                "",
                f"👤 <b>Пользователь:</b> {user.full_name}",
                f"🆔 <b>Telegram ID:</b> <code>{user.telegram_id}</code>",
                f"📱 <b>Username:</b> @{user.username or 'отсутствует'}",
                "",
                self._format_promo_group_block(new_group, title="Новая промогруппа", icon="🏆"),
            ]

            if old_group and old_group.id != new_group.id:
                message_lines.extend(
                    [
                        "",
                        self._format_promo_group_block(
                            old_group, title="Предыдущая промогруппа", icon="♻️"
                        ),
                    ]
                )

            if initiator_line:
                message_lines.extend(["", initiator_line])

            if reason:
                message_lines.extend(["", f"📝 Причина: {reason}"])

            message_lines.extend(
                [
                    "",
                    f"💰 Баланс пользователя: {settings.format_price(user.balance_kopeks)}",
                    f"⏰ <i>{datetime.now().strftime('%d.%m.%Y %H:%M:%S')}</i>",
                ]
            )

            return await self._send_message("\n".join(message_lines))

        except Exception as e:
            logger.error(f"Ошибка отправки уведомления о смене промогруппы: {e}")
            return False

    async def _send_message(self, text: str, reply_markup: types.InlineKeyboardMarkup | None = None, *, ticket_event: bool = False) -> bool:
        if not self.chat_id:
            logger.warning("ADMIN_NOTIFICATIONS_CHAT_ID не настроен")
            return False
        
        try:
            message_kwargs = {
                'chat_id': self.chat_id,
                'text': text,
                'parse_mode': 'HTML',
                'disable_web_page_preview': True
            }
            
            # route to ticket-specific topic if provided
            thread_id = None
            if ticket_event and self.ticket_topic_id:
                thread_id = self.ticket_topic_id
            elif self.topic_id:
                thread_id = self.topic_id
            if thread_id:
                message_kwargs['message_thread_id'] = thread_id
            if reply_markup is not None:
                message_kwargs['reply_markup'] = reply_markup
            
            await self.bot.send_message(**message_kwargs)
            logger.info(f"Уведомление отправлено в чат {self.chat_id}")
            return True
            
        except TelegramForbiddenError:
            logger.error(f"Бот не имеет прав для отправки в чат {self.chat_id}")
            return False
        except TelegramBadRequest as e:
            logger.error(f"Ошибка отправки уведомления: {e}")
            return False
        except Exception as e:
            logger.error(f"Неожиданная ошибка при отправке уведомления: {e}")
            return False
    
    def _is_enabled(self) -> bool:
        return self.enabled and bool(self.chat_id)
    
    def _get_payment_method_display(self, payment_method: Optional[str]) -> str:
        method_names = {
            'telegram_stars': '⭐ Telegram Stars',
            'yookassa': '💳 YooKassa (карта)',
            'tribute': '💎 Tribute (карта)',
            'mulenpay': '💳 Mulen Pay (карта)',
            'pal24': '🏦 PayPalych (СБП)',
            'manual': '🛠️ Вручную (админ)',
            'balance': '💰 С баланса'
        }
        
        if not payment_method:
            return '💰 С баланса'
            
        return method_names.get(payment_method, '💰 С баланса')
    
    def _format_traffic(self, traffic_gb: int) -> str:
        if traffic_gb == 0:
            return "∞ Безлимит"
        return f"{traffic_gb} ГБ"
    
    def _get_subscription_status(self, subscription: Optional[Subscription]) -> str:
        if not subscription:
            return "❌ Нет подписки"

        if subscription.is_trial:
            return f"🎯 Триал (до {subscription.end_date.strftime('%d.%m')})"
        elif subscription.is_active:
            return f"✅ Активна (до {subscription.end_date.strftime('%d.%m')})"
        else:
            return "❌ Неактивна"
    
    async def _get_servers_info(self, squad_uuids: list) -> str:
        if not squad_uuids:
            return "❌ Нет серверов"
        
        try:
            from app.handlers.subscription import get_servers_display_names
            servers_names = await get_servers_display_names(squad_uuids)
            return f"{len(squad_uuids)} шт. ({servers_names})"
        except Exception as e:
            logger.warning(f"Не удалось получить названия серверов: {e}")
            return f"{len(squad_uuids)} шт."


    async def send_maintenance_status_notification(
        self,
        event_type: str,
        status: str,
        details: Dict[str, Any] = None
    ) -> bool:
        if not self._is_enabled():
            return False
        
        try:
            details = details or {}
            
            if event_type == "enable":
                if details.get("auto_enabled", False):
                    icon = "⚠️"
                    title = "АВТОМАТИЧЕСКОЕ ВКЛЮЧЕНИЕ ТЕХРАБОТ"
                else:
                    icon = "🔧"
                    title = "ВКЛЮЧЕНИЕ ТЕХРАБОТ"
                    
            elif event_type == "disable":
                icon = "✅"
                title = "ОТКЛЮЧЕНИЕ ТЕХРАБОТ"
                
            elif event_type == "api_status":
                if status == "online":
                    icon = "🟢"
                    title = "API REMNAWAVE ВОССТАНОВЛЕНО"
                else:
                    icon = "🔴"
                    title = "API REMNAWAVE НЕДОСТУПНО"
                    
            elif event_type == "monitoring":
                if status == "started":
                    icon = "🔍"
                    title = "МОНИТОРИНГ ЗАПУЩЕН"
                else:
                    icon = "⏹️"
                    title = "МОНИТОРИНГ ОСТАНОВЛЕН"
            else:
                icon = "ℹ️"
                title = "СИСТЕМА ТЕХРАБОТ"
            
            message_parts = [f"{icon} <b>{title}</b>", ""]
            
            if event_type == "enable":
                if details.get("reason"):
                    message_parts.append(f"📋 <b>Причина:</b> {details['reason']}")
                
                if details.get("enabled_at"):
                    enabled_at = details["enabled_at"]
                    if isinstance(enabled_at, str):
                        from datetime import datetime
                        enabled_at = datetime.fromisoformat(enabled_at)
                    message_parts.append(f"🕐 <b>Время включения:</b> {enabled_at.strftime('%d.%m.%Y %H:%M:%S')}")
                
                message_parts.append(f"🤖 <b>Автоматически:</b> {'Да' if details.get('auto_enabled', False) else 'Нет'}")
                message_parts.append("")
                message_parts.append("❗ Обычные пользователи временно не могут использовать бота.")
                
            elif event_type == "disable":
                if details.get("disabled_at"):
                    disabled_at = details["disabled_at"]
                    if isinstance(disabled_at, str):
                        from datetime import datetime
                        disabled_at = datetime.fromisoformat(disabled_at)
                    message_parts.append(f"🕐 <b>Время отключения:</b> {disabled_at.strftime('%d.%m.%Y %H:%M:%S')}")
                
                if details.get("duration"):
                    duration = details["duration"]
                    if isinstance(duration, (int, float)):
                        hours = int(duration // 3600)
                        minutes = int((duration % 3600) // 60)
                        if hours > 0:
                            duration_str = f"{hours}ч {minutes}мин"
                        else:
                            duration_str = f"{minutes}мин"
                        message_parts.append(f"⏱️ <b>Длительность:</b> {duration_str}")
                
                message_parts.append(f"🤖 <b>Было автоматическим:</b> {'Да' if details.get('was_auto', False) else 'Нет'}")
                message_parts.append("")
                message_parts.append("✅ Сервис снова доступен для пользователей.")
                
            elif event_type == "api_status":
                message_parts.append(f"🔗 <b>API URL:</b> {details.get('api_url', 'неизвестно')}")
                
                if status == "online":
                    if details.get("response_time"):
                        message_parts.append(f"⚡ <b>Время отклика:</b> {details['response_time']} сек")
                        
                    if details.get("consecutive_failures", 0) > 0:
                        message_parts.append(f"🔄 <b>Неудачных попыток было:</b> {details['consecutive_failures']}")
                        
                    message_parts.append("")
                    message_parts.append("API снова отвечает на запросы.")
                    
                else: 
                    if details.get("consecutive_failures"):
                        message_parts.append(f"🔄 <b>Попытка №:</b> {details['consecutive_failures']}")
                        
                    if details.get("error"):
                        error_msg = str(details["error"])[:100]  
                        message_parts.append(f"❌ <b>Ошибка:</b> {error_msg}")
                        
                    message_parts.append("")
                    message_parts.append("⚠️ Началась серия неудачных проверок API.")
                    
            elif event_type == "monitoring":
                if status == "started":
                    if details.get("check_interval"):
                        message_parts.append(f"🔄 <b>Интервал проверки:</b> {details['check_interval']} сек")
                        
                    if details.get("auto_enable_configured") is not None:
                        auto_enable = "Включено" if details["auto_enable_configured"] else "Отключено"
                        message_parts.append(f"🤖 <b>Автовключение:</b> {auto_enable}")
                        
                    if details.get("max_failures"):
                        message_parts.append(f"🎯 <b>Порог ошибок:</b> {details['max_failures']}")
                        
                    message_parts.append("")
                    message_parts.append("Система будет следить за доступностью API.")
                    
                else:  
                    message_parts.append("Автоматический мониторинг API остановлен.")
            
            from datetime import datetime
            message_parts.append("")
            message_parts.append(f"⏰ <i>{datetime.now().strftime('%d.%m.%Y %H:%M:%S')}</i>")
            
            message = "\n".join(message_parts)
            
            return await self._send_message(message)
            
        except Exception as e:
            logger.error(f"Ошибка отправки уведомления о техработах: {e}")
            return False
    
    async def send_remnawave_panel_status_notification(
        self,
        status: str,
        details: Dict[str, Any] = None
    ) -> bool:
        if not self._is_enabled():
            return False
        
        try:
            details = details or {}
            
            status_config = {
                "online": {"icon": "🟢", "title": "ПАНЕЛЬ REMNAWAVE ДОСТУПНА", "alert_type": "success"},
                "offline": {"icon": "🔴", "title": "ПАНЕЛЬ REMNAWAVE НЕДОСТУПНА", "alert_type": "error"},
                "degraded": {"icon": "🟡", "title": "ПАНЕЛЬ REMNAWAVE РАБОТАЕТ СО СБОЯМИ", "alert_type": "warning"},
                "maintenance": {"icon": "🔧", "title": "ПАНЕЛЬ REMNAWAVE НА ОБСЛУЖИВАНИИ", "alert_type": "info"}
            }
            
            config = status_config.get(status, status_config["offline"])
            
            message_parts = [
                f"{config['icon']} <b>{config['title']}</b>",
                ""
            ]
            
            if details.get("api_url"):
                message_parts.append(f"🔗 <b>URL:</b> {details['api_url']}")
                
            if details.get("response_time"):
                message_parts.append(f"⚡ <b>Время отклика:</b> {details['response_time']} сек")
                
            if details.get("last_check"):
                last_check = details["last_check"]
                if isinstance(last_check, str):
                    from datetime import datetime
                    last_check = datetime.fromisoformat(last_check)
                message_parts.append(f"🕐 <b>Последняя проверка:</b> {last_check.strftime('%H:%M:%S')}")
                
            if status == "online":
                if details.get("uptime"):
                    message_parts.append(f"⏱️ <b>Время работы:</b> {details['uptime']}")
                    
                if details.get("users_online"):
                    message_parts.append(f"👥 <b>Пользователей онлайн:</b> {details['users_online']}")
                    
                message_parts.append("")
                message_parts.append("✅ Все системы работают нормально.")
                
            elif status == "offline":
                if details.get("error"):
                    error_msg = str(details["error"])[:150]
                    message_parts.append(f"❌ <b>Ошибка:</b> {error_msg}")
                    
                if details.get("consecutive_failures"):
                    message_parts.append(f"🔄 <b>Неудачных попыток:</b> {details['consecutive_failures']}")
                    
                message_parts.append("")
                message_parts.append("⚠️ Панель недоступна. Проверьте соединение и статус сервера.")
                
            elif status == "degraded":
                if details.get("issues"):
                    issues = details["issues"]
                    if isinstance(issues, list):
                        message_parts.append("⚠️ <b>Обнаруженные проблемы:</b>")
                        for issue in issues[:3]: 
                            message_parts.append(f"   • {issue}")
                    else:
                        message_parts.append(f"⚠️ <b>Проблема:</b> {issues}")
                        
                message_parts.append("")
                message_parts.append("Панель работает, но возможны задержки или сбои.")
                
            elif status == "maintenance":
                if details.get("maintenance_reason"):
                    message_parts.append(f"🔧 <b>Причина:</b> {details['maintenance_reason']}")
                    
                if details.get("estimated_duration"):
                    message_parts.append(f"⏰ <b>Ожидаемая длительность:</b> {details['estimated_duration']}")
                    
                message_parts.append("")
                message_parts.append("Панель временно недоступна для обслуживания.")
            
            from datetime import datetime
            message_parts.append("")
            message_parts.append(f"⏰ <i>{datetime.now().strftime('%d.%m.%Y %H:%M:%S')}</i>")
            
            message = "\n".join(message_parts)
            
            return await self._send_message(message)
            
        except Exception as e:
            logger.error(f"Ошибка отправки уведомления о статусе панели Remnawave: {e}")
            return False

    async def send_subscription_update_notification(
        self,
        db: AsyncSession,
        user: User,
        subscription: Subscription,
        update_type: str,
        old_value: Any,
        new_value: Any,
        price_paid: int = 0
    ) -> bool:
        if not self._is_enabled():
            return False
        
        try:
            referrer_info = await self._get_referrer_info(db, user.referred_by_id)
            promo_group = await self._get_user_promo_group(db, user)
            promo_block = self._format_promo_group_block(promo_group)

            update_types = {
                "traffic": ("📊 ИЗМЕНЕНИЕ ТРАФИКА", "трафик"),
                "devices": ("📱 ИЗМЕНЕНИЕ УСТРОЙСТВ", "количество устройств"),
                "servers": ("🌐 ИЗМЕНЕНИЕ СЕРВЕРОВ", "серверы")
            }

            title, param_name = update_types.get(update_type, ("⚙️ ИЗМЕНЕНИЕ ПОДПИСКИ", "параметры"))

            message_lines = [
                f"{title}",
                "",
                f"👤 <b>Пользователь:</b> {user.full_name}",
                f"🆔 <b>Telegram ID:</b> <code>{user.telegram_id}</code>",
                f"📱 <b>Username:</b> @{user.username or 'отсутствует'}",
                "",
                promo_block,
                "",
                "🔧 <b>Изменение:</b>",
                f"📋 Параметр: {param_name}",
            ]

            if update_type == "servers":
                old_servers_info = await self._format_servers_detailed(old_value)
                new_servers_info = await self._format_servers_detailed(new_value)
                message_lines.extend(
                    [
                        f"📉 Было: {old_servers_info}",
                        f"📈 Стало: {new_servers_info}",
                    ]
                )
            else:
                message_lines.extend(
                    [
                        f"📉 Было: {self._format_update_value(old_value, update_type)}",
                        f"📈 Стало: {self._format_update_value(new_value, update_type)}",
                    ]
                )

            if price_paid > 0:
                message_lines.append(f"💰 Доплачено: {settings.format_price(price_paid)}")
            else:
                message_lines.append("💸 Бесплатно")

            message_lines.extend(
                [
                    "",
                    f"📅 <b>Подписка действует до:</b> {subscription.end_date.strftime('%d.%m.%Y %H:%M')}",
                    f"💰 <b>Баланс после операции:</b> {settings.format_price(user.balance_kopeks)}",
                    f"🔗 <b>Рефер:</b> {referrer_info}",
                    "",
                    f"⏰ <i>{datetime.now().strftime('%d.%m.%Y %H:%M:%S')}</i>",
                ]
            )

            return await self._send_message("\n".join(message_lines))
            
        except Exception as e:
            logger.error(f"Ошибка отправки уведомления об изменении подписки: {e}")
            return False

    async def _format_servers_detailed(self, server_uuids: List[str]) -> str:
        if not server_uuids:
            return "Нет серверов"
        
        try:
            from app.handlers.subscription import get_servers_display_names
            servers_names = await get_servers_display_names(server_uuids)
            
            if servers_names and servers_names != "Нет серверов":
                return f"{len(server_uuids)} серверов ({servers_names})"
            else:
                return f"{len(server_uuids)} серверов"
                
        except Exception as e:
            logger.warning(f"Ошибка получения названий серверов для уведомления: {e}")
            return f"{len(server_uuids)} серверов"

    def _format_update_value(self, value: Any, update_type: str) -> str:
        if update_type == "traffic":
            if value == 0:
                return "♾ Безлимитный"
            return f"{value} ГБ"
        elif update_type == "devices":
            return f"{value} устройств"
        elif update_type == "servers":
            if isinstance(value, list):
                return f"{len(value)} серверов"
            return str(value)
        return str(value)

    async def send_ticket_event_notification(
        self,
        text: str,
        keyboard: types.InlineKeyboardMarkup | None = None
    ) -> bool:
        """Публичный метод для отправки уведомлений по тикетам в админ-топик.
        Учитывает настройки включенности в settings.
        """
        # Respect runtime toggle for admin ticket notifications
        try:
            from app.services.support_settings_service import SupportSettingsService
            runtime_enabled = SupportSettingsService.get_admin_ticket_notifications_enabled()
        except Exception:
            runtime_enabled = True
        if not (self._is_enabled() and runtime_enabled):
            return False
        return await self._send_message(text, reply_markup=keyboard, ticket_event=True)

