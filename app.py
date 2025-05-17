import asyncio
import uuid
import matplotlib.pyplot as plt
import numpy as np
import io
from datetime import datetime, timedelta, timezone
from statistics import mean
import json  # <--- Добавлено для RabbitMQ

import aiohttp
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import ReplyKeyboardBuilder, InlineKeyboardBuilder
from aiogram.fsm.storage.memory import MemoryStorage  # <--- Для FSM (рекомендуется)
from aiogram.fsm.context import FSMContext  # <--- Для FSM (рекомендуется)
from aiogram.fsm.state import State, StatesGroup  # <--- Для FSM (рекомендуется)

import aio_pika  # <--- Добавлено для RabbitMQ

# --- НАСТРОЙКИ ---
BOT_TOKEN = '8172347160:AAFhPQZ4UBaQSwIWIXxCkVR-FP-j32JhGqU'  # Замени на свой токен
API_URL_USERS = 'http://localhost:5268/api/users'
API_URL_MEASUREMENTS = 'http://localhost:5268/api/measurements'

# Настройки RabbitMQ
RABBITMQ_URL = "amqp://user:password@localhost/"  # Если RabbitMQ локально и бот не в докере
# Если бот и RabbitMQ в Docker Compose в одной сети, используй имя сервиса:
# RABBITMQ_URL = "amqp://user:password@rabbitmq/"
MEASUREMENT_QUEUE_NAME = "weight_measurements_queue"  # Имя очереди

# Инициализация бота и диспетчера
# Рекомендуется использовать FSM для управления состояниями, например, ожидания ввода веса
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
bot = Bot(token=BOT_TOKEN)


# --- СОСТОЯНИЯ FSM (для ввода веса) ---
class WeightInputStates(StatesGroup):
    awaiting_weight = State()


# --- ОБЩИЕ ФУНКЦИИ ---
async def create_session():
    return aiohttp.ClientSession()


async def handle_api_error(response, message_or_callback: types.Message | types.CallbackQuery,
                           default_error="Ошибка при обращении к серверу"):
    # (Этот код остается таким же, как в твоем исходном файле)
    # Убедись, что он правильно обрабатывает и message, и callback_query для вывода ответа
    target_to_answer = message_or_callback.message if isinstance(message_or_callback,
                                                                 types.CallbackQuery) else message_or_callback
    status = response.status

    if status == 500:
        error_text = f"❌ Ошибка сервера (500).\n\n" \
                     f"Похоже, что сервер не может подключиться к базе данных PostgreSQL. " \
                     f"Проверьте, запущен ли PostgreSQL и настройки подключения."
        await target_to_answer.answer(error_text)
        return True

    elif status == 404:
        await target_to_answer.answer(f"❌ Ресурс не найден (404).")
        return True

    elif status == 400:
        try:
            error_data = await response.json()
            error_message = error_data.get('message', 'Неверный запрос')
            errors = error_data.get('errors', [])
            error_text = f"❌ Ошибка в запросе (400): {error_message}\n"

            if errors:
                error_text += "\nДетали ошибки:\n"
                for error_item in errors:  # Изменено с error на error_item, чтобы не конфликтовать
                    error_text += f"- {error_item}\n"

            await target_to_answer.answer(error_text)
        except:
            await target_to_answer.answer(f"❌ Ошибка в запросе (400). Проверьте формат данных.")
        return True

    elif status >= 200 and status < 300:  # Успешные статусы, но не 200 ОК (например 201, 204)
        return False  # Ошибки нет, но это не обязательно 200 ОК

    elif status != 200:  # Все остальные ошибки
        try:
            error_data = await response.json()
            error_message = error_data.get('message', default_error)
            await target_to_answer.answer(f"❌ {error_message} (Код: {status})")
        except:
            await target_to_answer.answer(f"❌ {default_error} (Код: {status})")
        return True

    return False  # Ошибки нет (для статуса 200)


def get_main_keyboard():
    builder = ReplyKeyboardBuilder()
    builder.add(KeyboardButton(text="📝 Добавить вес"))
    builder.add(KeyboardButton(text="📊 Мои измерения"))
    builder.add(KeyboardButton(text="📈 Статистика"))
    builder.add(KeyboardButton(text="📉 График"))
    builder.adjust(2)
    return builder.as_markup(resize_keyboard=True)


# --- ОБРАБОТЧИКИ КОМАНД И КНОПОК ---

@dp.message(Command("start"))
async def start_command(message: types.Message):
    try:
        async with await create_session() as session:
            tg_id = message.from_user.id
            url_to_get_user = f'{API_URL_USERS}/{tg_id}'

            async with session.get(url_to_get_user) as response:
                if response.status == 200:
                    user_data = await response.json()
                    await message.answer(
                        f"Добро пожаловать, {user_data.get('name', 'пользователь')}!",
                        reply_markup=get_main_keyboard()
                    )
                elif response.status == 404:
                    user_data_to_create = {
                        "id": str(uuid.uuid4()),
                        "tgId": str(tg_id),
                        "name": message.from_user.full_name,
                        "registrationTime": datetime.utcnow().isoformat() + "Z"
                    }
                    url_to_create_user = API_URL_USERS
                    async with session.post(url_to_create_user, json=user_data_to_create) as create_response:
                        if create_response.status == 200 or create_response.status == 201:
                            await message.answer(
                                "Добро пожаловать! Вы успешно зарегистрированы.",
                                reply_markup=get_main_keyboard()
                            )
                        else:
                            if await handle_api_error(create_response, message, "Ошибка при регистрации пользователя"):
                                return
                            await message.answer(
                                f"Произошла неожиданная ошибка при регистрации (статус: {create_response.status}).")
                else:
                    if await handle_api_error(response, message, "Ошибка при поиске пользователя"):
                        return
                    await message.answer(
                        f"Произошла неожиданная ошибка при поиске пользователя (статус: {response.status}).")
    except aiohttp.ClientConnectorError:
        await message.answer(
            f"❌ Не удалось подключиться к серверу API ({API_URL_USERS}). Проверьте, что сервер запущен.",
            reply_markup=get_main_keyboard()  # Добавил клавиатуру и здесь
        )
    except Exception as e:
        await message.answer(f"Произошла критическая ошибка: {str(e)}", reply_markup=get_main_keyboard())


# Обработчик кнопки "Добавить вес" - теперь устанавливает состояние FSM
@dp.message(F.text == "📝 Добавить вес")
async def add_weight_prompt(message: types.Message, state: FSMContext):
    await message.answer("Введите ваш текущий вес в кг (например: 75.5):")
    await state.set_state(WeightInputStates.awaiting_weight)


# Обработчик ввода веса (реагирует на состояние awaiting_weight)
@dp.message(WeightInputStates.awaiting_weight, F.text)
async def process_weight_input_to_rabbitmq(message: types.Message, state: FSMContext):
    try:
        weight = float(message.text.replace(',', '.'))
        if not (0 < weight <= 300):  # Более строгое условие
            await message.answer("Пожалуйста, введите корректный вес (больше 0 и не более 300 кг):")
            # Состояние не сбрасываем, пользователь может попробовать еще раз
            return

        tg_id = str(message.from_user.id)
        measurement_data = {
            "weight": weight,
            "tgId": tg_id,
            "messageTimestamp": datetime.utcnow().isoformat() + "Z"  # Дополнительная информация
        }

        try:
            connection = await aio_pika.connect_robust(RABBITMQ_URL)
            async with connection:
                channel = await connection.channel()
                await channel.declare_queue(MEASUREMENT_QUEUE_NAME, durable=True)
                await channel.default_exchange.publish(
                    aio_pika.Message(
                        body=json.dumps(measurement_data).encode(),
                        delivery_mode=aio_pika.DeliveryMode.PERSISTENT
                    ),
                    routing_key=MEASUREMENT_QUEUE_NAME,
                )
            print(f"Сообщение для tg_id {tg_id} с весом {weight} отправлено в очередь {MEASUREMENT_QUEUE_NAME}")
            await message.answer(
                f"✅ Ваш вес {weight} кг принят на обработку!",
                reply_markup=get_main_keyboard()
            )
        except aio_pika.exceptions.AMQPConnectionError as e_amqp:
            print(f"Ошибка подключения к RabbitMQ: {e_amqp}")
            await message.answer(
                "❌ Не удалось отправить данные на обработку (проблема с сервисом очереди).\nПожалуйста, попробуйте позже.",
                reply_markup=get_main_keyboard()
            )
        except Exception as e_publish:
            print(f"Ошибка при публикации в RabbitMQ: {e_publish}")
            await message.answer(
                f"❌ Произошла ошибка при отправке данных: {str(e_publish)}",
                reply_markup=get_main_keyboard()
            )

    except ValueError:
        await message.answer("Пожалуйста, введите корректное числовое значение веса:")
        return  # Остаемся в состоянии ожидания веса
    except Exception as e_outer:
        print(f"Критическая ошибка в process_weight_input_to_rabbitmq: {e_outer}")
        await message.answer(f"Произошла непредвиденная ошибка: {str(e_outer)}", reply_markup=get_main_keyboard())

    await state.clear()  # Сбрасываем состояние после успешной отправки или критической ошибки


# --- Остальные обработчики (Мои измерения, Статистика, График) остаются без изменений, ---
# --- так как они используют HTTP GET запросы к API и не связаны с RabbitMQ напрямую. ---

# Обработчик кнопки "Мои измерения"
@dp.message(F.text == "📊 Мои измерения")
async def show_measurements_command(message: types.Message):
    tg_id = message.from_user.id
    try:
        async with await create_session() as session:
            async with session.get(f'{API_URL_MEASUREMENTS}/user/{tg_id}') as response:
                if await handle_api_error(response, message, "Ошибка при получении измерений"):
                    return
                if response.status == 200:  # Убедимся, что это 200 перед .json()
                    measurements = await response.json()
                    if not measurements:
                        await message.answer("У вас пока нет измерений веса.", reply_markup=get_main_keyboard())
                        return

                    measurements.sort(key=lambda x: x['date'], reverse=True)
                    text = "Ваши последние измерения веса:\n\n"

                    builder = InlineKeyboardBuilder()
                    for i, m in enumerate(measurements[:5], 1):  # Покажем только 5 последних
                        # Проверяем формат даты, API возвращает ISO 8601 с 'Z'
                        try:
                            date_obj = datetime.fromisoformat(m['date'].replace('Z', '+00:00'))
                            formatted_date = date_obj.strftime("%d.%m.%Y %H:%M")
                        except ValueError:
                            formatted_date = m['date']  # Если формат неожиданный, покажем как есть

                        text += f"{i}. {formatted_date}: {m['weight']} кг\n"
                        builder.add(InlineKeyboardButton(
                            text=f"Удалить #{i} ({m['weight']} кг)",  # Добавим вес для ясности
                            callback_data=f"delete_measurement:{m['id']}"
                        ))
                    builder.adjust(1)  # По одной кнопке в ряд для лучшей читаемости

                    await message.answer(text,
                                         reply_markup=builder.as_markup() if measurements[:5] else get_main_keyboard())
                # else: уже обработано в handle_api_error
                #    await message.answer("Не удалось получить данные о измерениях.", reply_markup=get_main_keyboard())
    except aiohttp.ClientConnectorError:
        await message.answer(f"❌ Не удалось подключиться к API ({API_URL_MEASUREMENTS}).",
                             reply_markup=get_main_keyboard())
    except Exception as e:
        await message.answer(f"Ошибка при получении данных: {str(e)}", reply_markup=get_main_keyboard())


# Обработчик кнопки удаления измерения
@dp.callback_query(F.data.startswith('delete_measurement:'))
async def delete_measurement_callback(
        callback: types.CallbackQuery):  # Переименовал, чтобы не было конфликта с импортом
    measurement_id = callback.data.split(':')[1]
    try:
        async with await create_session() as session:
            async with session.delete(f'{API_URL_MEASUREMENTS}/{measurement_id}') as response:
                if response.status == 200:  # Успешное удаление (часто 204 No Content, но твой API возвращает 200)
                    await callback.answer("✅ Измерение удалено!", show_alert=False)
                    # Обновляем сообщение, убирая удаленный элемент (если это последнее сообщение с измерениями)
                    # Это может быть сложно, проще попросить пользователя заново нажать "Мои измерения"
                    # или просто изменить текст на "Удалено. Обновите список."
                    await callback.message.edit_text(
                        f"{callback.message.text}\n\n✅ Измерение с ID ...{measurement_id[-6:]} удалено.\nОбновите список командой '📊 Мои измерения'.",
                        reply_markup=None  # Убираем инлайн клавиатуру после удаления
                    )
                else:
                    # Используем handle_api_error, если он может отправлять ответ на callback_query
                    # Пока просто:
                    error_data = await response.json() if response.content_type == 'application/json' else {
                        "message": await response.text()}
                    await callback.answer(
                        f"Ошибка при удалении: {error_data.get('message', 'Неизвестная ошибка API')}",
                        show_alert=True
                    )
    except aiohttp.ClientConnectorError:
        await callback.answer(f"❌ Не удалось подключиться к API ({API_URL_MEASUREMENTS}).", show_alert=True)
    except Exception as e:
        await callback.answer(f"Ошибка: {str(e)}", show_alert=True)


# Обработчик кнопки "Статистика"
@dp.message(F.text == "📈 Статистика")
async def show_statistics_command(message: types.Message):
    tg_id = message.from_user.id
    try:
        async with await create_session() as session:
            async with session.get(f'{API_URL_MEASUREMENTS}/user/{tg_id}') as response:
                if await handle_api_error(response, message, "Ошибка при получении статистики"):
                    return

                if response.status == 200:
                    measurements = await response.json()
                    if not measurements or len(measurements) < 1:  # Нужно хотя бы одно измерение
                        await message.answer("У вас пока нет измерений веса для формирования статистики.",
                                             reply_markup=get_main_keyboard())
                        return

                    measurements.sort(key=lambda x: x['date'])
                    weights = [m['weight'] for m in measurements]
                    dates_str = [m['date'] for m in measurements]

                    # Преобразуем даты в datetime объекты с UTC timezone
                    dates = []
                    for d_str in dates_str:
                        try:
                            dates.append(datetime.fromisoformat(d_str.replace('Z', '+00:00')))
                        except ValueError:
                            # Попытка распарсить, если Z уже нет или другой формат (менее вероятно из твоего API)
                            try:
                                dates.append(datetime.fromisoformat(d_str).replace(tzinfo=timezone.utc))
                            except ValueError:
                                await message.answer(f"Ошибка формата даты в данных: {d_str}",
                                                     reply_markup=get_main_keyboard())
                                return

                    current_weight = weights[-1]
                    initial_weight = weights[0]
                    weight_change = current_weight - initial_weight
                    avg_weight = mean(weights) if weights else 0
                    min_weight = min(weights) if weights else 0
                    max_weight = max(weights) if weights else 0

                    text = "📊 **Статистика измерений веса**\n\n"  # Markdown можно включить parse_mode='MarkdownV2'
                    text += f"📌 Текущий вес: {current_weight} кг\n"
                    text += f"📌 Начальный вес: {initial_weight} кг (за весь период)\n"
                    text += f"➖ Изменение: {weight_change:+.1f} кг\n"  # знак + для положительных
                    text += f"📊 Средний вес: {avg_weight:.1f} кг\n"
                    text += f"📉 Минимальный вес: {min_weight} кг\n"
                    text += f"📈 Максимальный вес: {max_weight} кг\n"
                    text += f"🔢 Всего измерений: {len(weights)}\n"

                    if len(dates) > 1:
                        first_date = dates[0]
                        last_date = dates[-1]
                        days_tracked = (last_date - first_date).days
                        text += f"🗓 Период отслеживания: с {first_date.strftime('%d.%m.%Y')} по {last_date.strftime('%d.%m.%Y')} ({days_tracked} дней)\n"
                        if days_tracked > 0 and len(weights) > 1:
                            avg_change_per_day = weight_change / days_tracked
                            text += f"⚖️ Среднее изменение в день: {avg_change_per_day:+.2f} кг/день\n"

                    await message.answer(text,
                                         reply_markup=get_main_keyboard())  # parse_mode="MarkdownV2" (нужно экранировать символы)
                # else: обработано в handle_api_error
    except aiohttp.ClientConnectorError:
        await message.answer(f"❌ Не удалось подключиться к API ({API_URL_MEASUREMENTS}).",
                             reply_markup=get_main_keyboard())
    except Exception as e:
        print(f"Ошибка в статистике: {e}")
        await message.answer(f"Ошибка при расчете статистики: {str(e)}", reply_markup=get_main_keyboard())


# Обработчик кнопки "График"
@dp.message(F.text == "📉 График")
async def show_graph_command(message: types.Message):
    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(text="Неделя", callback_data="graph:7"))
    builder.add(InlineKeyboardButton(text="Месяц", callback_data="graph:30"))
    builder.add(InlineKeyboardButton(text="3 месяца", callback_data="graph:90"))
    builder.add(InlineKeyboardButton(text="Всё время", callback_data="graph:all"))
    builder.adjust(2)
    await message.answer("Выберите период для отображения графика:", reply_markup=builder.as_markup())


# Обработчик выбора периода графика
@dp.callback_query(F.data.startswith('graph:'))
async def generate_graph_callback(callback: types.CallbackQuery):  # Переименовал
    period_str = callback.data.split(':')[1]
    tg_id = callback.from_user.id

    await callback.answer("⏳ Генерируем график...")  # Ответ на callback, чтобы кнопка не "висела"

    try:
        async with await create_session() as session:
            async with session.get(f'{API_URL_MEASUREMENTS}/user/{tg_id}') as response:
                if await handle_api_error(response, callback,
                                          "Ошибка при получении данных для графика"):  # Передаем callback
                    return

                if response.status == 200:
                    measurements = await response.json()
                    if not measurements:
                        await callback.message.answer("У вас пока нет измерений для построения графика.",
                                                      reply_markup=get_main_keyboard())
                        return

                    measurements.sort(key=lambda x: x['date'])

                    # Фильтрация по периоду
                    cutoff_date = None
                    if period_str != 'all':
                        days = int(period_str)
                        cutoff_date = datetime.now(timezone.utc) - timedelta(days=days)

                    filtered_measurements = measurements
                    if cutoff_date:
                        filtered_measurements = [
                            m for m in measurements
                            if datetime.fromisoformat(m['date'].replace('Z', '+00:00')) >= cutoff_date
                        ]

                    if not filtered_measurements:
                        await callback.message.answer(f"Нет данных за выбранный период.",
                                                      reply_markup=get_main_keyboard())
                        return

                    weights = [m['weight'] for m in filtered_measurements]
                    dates = [datetime.fromisoformat(m['date'].replace('Z', '+00:00')) for m in filtered_measurements]

                    if not dates or not weights:  # Доп. проверка
                        await callback.message.answer("Недостаточно данных для построения графика после фильтрации.",
                                                      reply_markup=get_main_keyboard())
                        return

                    plt.style.use('seaborn-v0_8-darkgrid')  # Попробуем стиль
                    fig, ax = plt.subplots(figsize=(12, 7))  # Увеличим размер

                    ax.plot(dates, weights, marker='o', linestyle='-', color='deepskyblue', linewidth=2, markersize=8,
                            label="Вес")

                    if len(dates) >= 2:  # Для тренда нужно хотя бы 2 точки
                        # Конвертируем даты в числовой формат для polyfit (секунды от первой даты)
                        numeric_dates = np.array([(d - dates[0]).total_seconds() for d in dates])
                        if len(np.unique(
                                numeric_dates)) >= 2:  # Убедимся, что есть хотя бы 2 уникальные точки по времени
                            coeffs = np.polyfit(numeric_dates, weights, 1)
                            poly = np.poly1d(coeffs)
                            trend_line = poly(numeric_dates)
                            ax.plot(dates, trend_line, "r--", alpha=0.7,
                                    label=f"Тренд ({coeffs[0] * 86400:.2f} кг/день)")  # 86400 секунд в дне
                            ax.legend(loc='best')

                    ax.set_title(f'Динамика веса (период: {period_str} {"дней" if period_str.isdigit() else ""})',
                                 fontsize=16)
                    ax.set_xlabel('Дата', fontsize=12)
                    ax.set_ylabel('Вес (кг)', fontsize=12)
                    ax.grid(True, which='both', linestyle='--', linewidth=0.5, alpha=0.7)

                    # Форматирование дат на оси X
                    from matplotlib.dates import DateFormatter
                    date_fmt = DateFormatter("%d.%m.%y")  # Более короткий формат
                    ax.xaxis.set_major_formatter(date_fmt)
                    fig.autofmt_xdate(rotation=30, ha='right')  # Автоматический наклон и выравнивание

                    # Аннотации значений
                    for i, (d, w) in enumerate(zip(dates, weights)):
                        ax.annotate(f"{w:.1f}", (d, w), textcoords="offset points", xytext=(0, 10), ha='center',
                                    fontsize=9)

                    min_y = min(weights) * 0.90
                    max_y = max(weights) * 1.10
                    ax.set_ylim(min_y, max_y)

                    plt.tight_layout(pad=1.5)  # Добавляем отступы

                    buf = io.BytesIO()
                    plt.savefig(buf, format='png', dpi=100)  # dpi для качества
                    buf.seek(0)
                    plt.close(fig)  # Закрываем фигуру plt

                    await callback.message.answer_photo(
                        types.BufferedInputFile(buf.getvalue(), filename="weight_chart.png"),
                        caption=f"График изменения веса за выбранный период."
                    )
                    # Удаляем инлайн клавиатуру после отправки графика
                    await callback.message.edit_reply_markup(reply_markup=None)


    except aiohttp.ClientConnectorError:
        await callback.message.answer(f"❌ Не удалось подключиться к API ({API_URL_MEASUREMENTS}).",
                                      reply_markup=get_main_keyboard())
    except Exception as e:
        print(f"Ошибка при построении графика: {e}")
        await callback.message.answer(f"Ошибка при построении графика: {str(e)}", reply_markup=get_main_keyboard())


# --- ЗАПУСК БОТА ---
async def main():
    # Код для создания таблиц (если он у тебя здесь был, лучше делать это на стороне API при старте)
    print("Запуск бота...")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()  # Закрываем сессию бота
        # Здесь можно добавить закрытие соединения с RabbitMQ, если оно глобальное, но мы открываем по запросу.


if __name__ == '__main__':
    asyncio.run(main())