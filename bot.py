"""
Утренняя сводка новостей в Telegram.

Что делает этот скрипт:
1. Скачивает RSS-ленты из списка FEEDS.
2. Оставляет только новости за последние 24 часа.
3. В каждой теме из TOPICS выбирает самые свежие (не больше MAX_PER_SOURCE с одного сайта).
4. Для каждой новости находит главную картинку и краткое описание.
5. Отправляет в Telegram одно сообщение: сверху большое фото главной новости,
   ниже все новости по темам, у каждой — пересказ, который раскрывается по нажатию.

Запуск:
    python bot.py            — собрать новости и отправить в Telegram
    python bot.py --dry-run  — только показать сообщение в терминале, ничего не отправлять
"""

import calendar
import html
import os
import re
import sys
from datetime import datetime, timedelta, timezone

import feedparser
import requests
from dotenv import load_dotenv

# ---------- Настройки ----------

# Темы — в этом порядке они идут в сообщении: (название, значок, сколько новостей)
TOPICS = [
    ("ИИ", "🤖", 3),
    ("Стартапы и венчур", "🚀", 3),
    ("Казахстан", "🇰🇿", 3),
    ("Футбол", "⚽", 3),
    ("UFC и бокс", "🥊", 3),
]

# Список лент: (название источника, тема из TOPICS, адрес RSS)
FEEDS = [
    # Искусственный интеллект
    ("TechCrunch AI", "ИИ", "https://techcrunch.com/category/artificial-intelligence/feed/"),
    ("MIT Technology Review", "ИИ", "https://www.technologyreview.com/topic/artificial-intelligence/feed"),
    ("Хабр: ИИ", "ИИ", "https://habr.com/ru/rss/hubs/artificial_intelligence/articles/"),
    # Стартапы и венчур
    ("TechCrunch Startups", "Стартапы и венчур", "https://techcrunch.com/category/startups/feed/"),
    ("Crunchbase News", "Стартапы и венчур", "https://news.crunchbase.com/feed/"),
    ("vc.ru", "Стартапы и венчур", "https://vc.ru/rss/all"),
    # Казахстан
    ("Курсив", "Казахстан", "https://kz.kursiv.media/feed/"),
    ("The Astana Times", "Казахстан", "https://astanatimes.com/feed/"),
    # Футбол: топ-5 лиг и сборные
    ("BBC Premier League", "Футбол", "https://feeds.bbci.co.uk/sport/football/premier-league/rss.xml"),
    ("BBC European Football", "Футбол", "https://feeds.bbci.co.uk/sport/football/european/rss.xml"),
    ("Football Italia", "Футбол", "https://www.football-italia.net/feed/"),
    # UFC и бокс
    ("Sherdog", "UFC и бокс", "https://www.sherdog.com/rss/news.xml"),
    ("BBC Boxing", "UFC и бокс", "https://feeds.bbci.co.uk/sport/boxing/rss.xml"),
    ("Чемпионат: единоборства", "UFC и бокс", "https://www.championat.com/rss/news/boxing/"),
]

# Новости, в заголовке которых есть эти слова, пропускаются (регистр не важен).
# Например, американские футбольные лиги, если они попадут в ленту.
EXCLUDE_WORDS = ["MLS", "NWSL", "USWNT"]

# Служебные фразы, которые некоторые сайты добавляют в описание — вырезаем их
BOILERPLATE = [
    r"The Astana Times provides news and information from Kazakhstan and around the world\.?",
    r"MIT Technology Review Explains:.*?You can read more from the series here\.",
    r"The post .* appeared first on .*",
    r"Читать дал(ее|ьше).*",
]

HOURS_BACK = 24        # за сколько часов брать новости
MAX_PER_SOURCE = 2     # максимум новостей с одного источника внутри темы
SUMMARY_MAX = 300      # максимальная длина пересказа в символах
MESSAGE_LIMIT = 4000   # Telegram принимает не больше 4096 символов в сообщении, оставляем запас

# Некоторые сайты не отдают ленту «безымянным» программам, поэтому представляемся
HEADERS = {"User-Agent": "Mozilla/5.0 (morning-news-bot)"}


# ---------- Сбор новостей ----------

def fetch_feed(source, topic, url):
    """Скачивает одну ленту и возвращает список новостей (словарей)."""
    response = requests.get(url, headers=HEADERS, timeout=20)
    response.raise_for_status()  # если сайт ответил ошибкой — сообщить об этом
    feed = feedparser.parse(response.content)

    news = []
    for entry in feed.entries:
        # feedparser сам разбирает дату публикации; берём её в UTC
        parsed = entry.get("published_parsed") or entry.get("updated_parsed")
        if not parsed or not entry.get("link"):
            continue  # без даты или ссылки новость нам не подходит
        news.append({
            "title": clean_text(entry.get("title", "")) or "(без заголовка)",
            "link": entry.link,
            "source": source,
            "topic": topic,
            "published": datetime.fromtimestamp(calendar.timegm(parsed), tz=timezone.utc),
            "summary": clean_text(entry.get("summary", "")),
            "image": feed_image(entry),
        })
    return news


def feed_image(entry):
    """Картинка новости прямо из ленты (если сайт её туда положил)."""
    for key in ("media_content", "media_thumbnail"):
        for media in entry.get(key) or []:
            if media.get("url"):
                return media["url"]
    for link in entry.get("links", []):
        if link.get("type", "").startswith("image"):
            return link.get("href")
    return None


def clean_text(text):
    """Убирает из текста HTML-теги, служебные фразы и лишние пробелы."""
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    for pattern in BOILERPLATE:
        text = re.sub(pattern, " ", text, flags=re.S)
    text = re.sub(r"\s+", " ", text).strip()
    return re.sub(r" ([,.;:!?’»)])", r"\1", text)  # пробел перед знаком препинания — лишний


def page_meta(page, name):
    """Достаёт из HTML-страницы тег <meta property="og:..." content="...">."""
    patterns = [
        rf'<meta[^>]+(?:property|name)=["\']{name}["\'][^>]*content=["\']([^"\']*)',
        rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]*(?:property|name)=["\']{name}["\']',
    ]
    for pattern in patterns:
        match = re.search(pattern, page, re.I)
        if match:
            return html.unescape(match.group(1)).strip()
    return None


def enrich(n):
    """Открывает страницу новости и берёт оттуда большую картинку и описание.

    Это те же данные, которые Telegram показывает, когда отправляешь ссылку.
    """
    try:
        response = requests.get(n["link"], headers=HEADERS, timeout=20)
        if response.encoding == "ISO-8859-1":  # сайт не указал кодировку — почти всегда это UTF-8
            response.encoding = "utf-8"
        page = response.text
    except Exception as error:
        print(f"  ✗ не удалось открыть страницу {n['link']} ({error})")
        return
    n["image"] = page_meta(page, "og:image") or n["image"]
    description = clean_text(page_meta(page, "og:description") or "")
    if len(description) > len(n["summary"]):  # берём описание подлиннее
        n["summary"] = description


def shorten(text, limit):
    """Обрезает текст до limit символов, стараясь закончить на конце предложения."""
    if text.endswith("..."):  # сайт сам обрезал описание — ставим одно многоточие
        text = text[:-3].rstrip() + "…"
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    if end > limit // 3:
        return cut[:end + 1]
    return cut[:cut.rfind(" ")] + "…"


def is_excluded(title):
    """True, если в заголовке есть одно из слов EXCLUDE_WORDS."""
    lower = title.lower()
    return any(word.lower() in lower for word in EXCLUDE_WORDS)


def collect_news():
    """Собирает новости со всех лент и в каждой теме отбирает самые свежие.

    Возвращает словарь: {тема: [новости]}.
    """
    all_news = []
    for source, topic, url in FEEDS:
        try:
            items = fetch_feed(source, topic, url)
            print(f"  ✓ {source}: {len(items)} новостей в ленте")
            all_news.extend(items)
        except Exception as error:
            # Одна сломанная лента не должна ронять весь бот
            print(f"  ✗ {source}: не удалось загрузить ({error})")

    # Только за последние HOURS_BACK часов и без исключённых слов
    since = datetime.now(timezone.utc) - timedelta(hours=HOURS_BACK)
    fresh = [n for n in all_news if n["published"] >= since and not is_excluded(n["title"])]

    # Сначала самые новые
    fresh.sort(key=lambda n: n["published"], reverse=True)

    # Раскладываем по темам. Убираем повторы (одна статья бывает в нескольких лентах)
    # и ограничиваем число новостей с одного источника
    by_topic = {name: [] for name, _, _ in TOPICS}
    limits = {name: limit for name, _, limit in TOPICS}
    seen_links, per_source = set(), {}
    for n in fresh:
        topic = n["topic"]
        if (
            n["link"] in seen_links
            or len(by_topic[topic]) >= limits[topic]
            or per_source.get(n["source"], 0) >= MAX_PER_SOURCE
        ):
            continue
        by_topic[topic].append(n)
        seen_links.add(n["link"])
        per_source[n["source"]] = per_source.get(n["source"], 0) + 1
    return by_topic


# ---------- Сообщения и отправка ----------

def build_message(by_topic, summary_max):
    """Собирает одно сообщение: раздел на каждую тему, у новости — заголовок,
    пересказ в раскрывающейся цитате, источник и ссылка."""
    today = datetime.now().strftime("%d.%m.%Y")
    if not any(by_topic.values()):
        return f"🗞 <b>Утренняя сводка — {today}</b>\n\nЗа последние {HOURS_BACK} ч свежих новостей не нашлось."

    lines = [f"☀️ <b>Доброе утро! Сводка на {today}</b>"]
    for topic, icon, _ in TOPICS:
        news = by_topic[topic]
        if not news:
            continue  # пустые разделы не показываем
        lines += ["", f"{icon} <b>{html.escape(topic).upper()}</b>"]
        for n in news:
            local_time = n["published"].astimezone().strftime("%H:%M")  # время по часовому поясу компьютера
            # html.escape нужен, чтобы символы вроде < и & в тексте не сломали разметку
            lines.append(f"\n▪️ <b>{html.escape(n['title'], quote=False)}</b>")
            summary = shorten(n["summary"], summary_max)
            if summary and summary.lower() != n["title"].lower():
                # expandable — Telegram сворачивает длинную цитату, она раскрывается по нажатию
                lines.append(f"<blockquote expandable>{html.escape(summary, quote=False)}</blockquote>")
            lines.append(f'<i>{html.escape(n["source"])} · {local_time}</i> · <a href="{html.escape(n["link"])}">читать →</a>')
    return "\n".join(lines)


def visible_length(message):
    """Сколько символов увидит Telegram: без HTML-тегов, эмодзи считаются как 2."""
    text = html.unescape(re.sub(r"<[^>]+>", "", message))
    return len(text.encode("utf-16-le")) // 2


def fit_message(by_topic):
    """Укорачивает пересказы, пока сообщение не влезет в лимит Telegram."""
    summary_max = SUMMARY_MAX
    message = build_message(by_topic, summary_max)
    while visible_length(message) > MESSAGE_LIMIT and summary_max > 0:
        summary_max -= 20
        message = build_message(by_topic, summary_max)
    return message


def top_news(by_topic):
    """Главная новость — первая по порядку тем, у которой есть картинка."""
    for topic, _, _ in TOPICS:
        for n in by_topic[topic]:
            if n["image"]:
                return n
    return None


def send_to_telegram(text, preview_link, token, chat_id):
    """Отправляет сообщение через Telegram Bot API.

    Фото сверху — это превью ссылки на главную новость: Telegram сам берёт картинку со страницы.
    """
    if preview_link:
        preview = {"url": preview_link, "prefer_large_media": True, "show_above_text": True}
    else:
        preview = {"is_disabled": True}
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    response = requests.post(url, json={
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",  # чтобы работали жирный шрифт, цитаты и ссылки
        "link_preview_options": preview,
    }, timeout=30)
    if not response.ok:
        raise RuntimeError(f"Telegram ответил ошибкой: {response.text}")


def main():
    dry_run = "--dry-run" in sys.argv

    print("Собираю новости...")
    news = collect_news()

    print("Ищу картинки и описания...")
    for items in news.values():
        for n in items:
            enrich(n)

    message = fit_message(news)
    top = top_news(news)
    preview_link = top["link"] if top else None

    if dry_run:
        print("\n--- Сообщение (не отправлено, режим --dry-run) ---\n")
        print(f"[фото сверху: превью {preview_link}]\n")
        print(message)
        print(f"\n(длина: {visible_length(message)} из {MESSAGE_LIMIT})")
        return

    # Читаем секреты из файла .env
    load_dotenv()
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("\nОшибка: заполните TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID в файле .env (см. README.md)")
        sys.exit(1)

    send_to_telegram(message, preview_link, token, chat_id)
    print(f"\nГотово! Отправлено новостей: {sum(len(items) for items in news.values())}")


if __name__ == "__main__":
    main()
