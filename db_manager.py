import sqlite3

DB_NAME = "agro_data.db"

def init_db():
    """Создает таблицы в локальном файле agro_data.db, если их еще нет"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    # Таблица пользователей
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            username TEXT PRIMARY KEY,
            password TEXT,
            region TEXT
        )
    """)
    
    # Таблица сохраненных расчетов калькулятора
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS calculations (
        username TEXT PRIMARY KEY,
        area REAL,
        density INTEGER,
        base_yield REAL,
        market_price REAL,
        medicine_price REAL,      -- оставляем для обратной совместимости,
                                -- фактически = цена exp_1
        risk_checked INTEGER,
        crop_name TEXT,           -- ← НОВОЕ
        FOREIGN KEY(username) REFERENCES users(username)
    )
    """)
    
    # миграция существующей БД: добавить crop_name, если её нет
    cursor.execute("PRAGMA table_info(calculations)")
    cols = [row[1] for row in cursor.fetchall()]
    if "crop_name" not in cols:
        cursor.execute("ALTER TABLE calculations ADD COLUMN crop_name TEXT")

    # новые таблицы
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS expense_overrides (
            username TEXT, expense_id INTEGER, price REAL,
            PRIMARY KEY (username, expense_id),
            FOREIGN KEY(username) REFERENCES users(username)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS custom_expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT, category_name TEXT, price REAL,
            FOREIGN KEY(username) REFERENCES users(username)
        )
    """)
        # персональные отметки рисков по культурам
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS risk_checks (
            username TEXT,
            risk_id TEXT,
            is_checked INTEGER DEFAULT 0,
            PRIMARY KEY (username, risk_id),
            FOREIGN KEY(username) REFERENCES users(username)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS expense_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT,
            category TEXT,
            name TEXT,
            price REAL DEFAULT 0,
            url TEXT DEFAULT '',
            source TEXT DEFAULT '',
            UNIQUE(username, category, name),
            FOREIGN KEY(username) REFERENCES users(username)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS dismissed_items (
            username TEXT,
            category TEXT,
            name TEXT,
            PRIMARY KEY (username, category, name),
            FOREIGN KEY(username) REFERENCES users(username)
        )
    """)

    conn.commit()
    conn.close()


def dismiss_expense_item(username, category, name) -> None:
    conn = sqlite3.connect(DB_NAME); cur = conn.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO dismissed_items (username, category, name) VALUES (?, ?, ?)",
        (username, category, name),
    )
    # также удаляем саму позицию, если она есть
    cur.execute(
        "DELETE FROM expense_items WHERE username = ? AND category = ? AND name = ?",
        (username, category, name),
    )
    conn.commit(); conn.close()


def load_dismissed_items(username) -> set[tuple[str, str]]:
    conn = sqlite3.connect(DB_NAME); cur = conn.cursor()
    cur.execute("SELECT category, name FROM dismissed_items WHERE username = ?", (username,))
    rows = cur.fetchall(); conn.close()
    return {(r[0], r[1]) for r in rows}


def save_expense_overrides(username, overrides: dict[int, float]):
    """overrides = {expense_id: price}"""
    conn = sqlite3.connect(DB_NAME); cur = conn.cursor()
    for exp_id, price in overrides.items():
        cur.execute("""
            INSERT INTO expense_overrides (username, expense_id, price)
            VALUES (?, ?, ?)
            ON CONFLICT(username, expense_id) DO UPDATE SET price=excluded.price
        """, (username, exp_id, price))
    conn.commit(); conn.close()


def load_expense_overrides(username) -> dict[int, float]:
    conn = sqlite3.connect(DB_NAME); cur = conn.cursor()
    cur.execute("SELECT expense_id, price FROM expense_overrides WHERE username = ?", (username,))
    rows = cur.fetchall(); conn.close()
    return {r[0]: r[1] for r in rows}


def save_risk_checks(username, checks: dict[str, bool]):
    """checks = {'strawberry_gray_mold': True, 'common_drought': False, ...}"""
    conn = sqlite3.connect(DB_NAME); cur = conn.cursor()
    for risk_id, checked in checks.items():
        cur.execute("""
            INSERT INTO risk_checks (username, risk_id, is_checked)
            VALUES (?, ?, ?)
            ON CONFLICT(username, risk_id) DO UPDATE SET is_checked=excluded.is_checked
        """, (username, risk_id, int(bool(checked))))
    conn.commit(); conn.close()


def load_risk_checks(username) -> dict[str, bool]:
    conn = sqlite3.connect(DB_NAME); cur = conn.cursor()
    cur.execute("SELECT risk_id, is_checked FROM risk_checks WHERE username = ?", (username,))
    rows = cur.fetchall(); conn.close()
    return {r[0]: bool(r[1]) for r in rows}


def load_custom_expenses(username) -> list[dict]:
    conn = sqlite3.connect(DB_NAME); cur = conn.cursor()
    cur.execute(
        "SELECT id, category_name, price FROM custom_expenses "
        "WHERE username = ? ORDER BY id",
        (username,),
    )
    rows = cur.fetchall(); conn.close()
    return [{"id": r[0], "cat": r[1], "price": r[2]} for r in rows]


def replace_custom_expenses(username, items: list[dict]) -> list[int]:
    conn = sqlite3.connect(DB_NAME); cur = conn.cursor()
    cur.execute("DELETE FROM custom_expenses WHERE username = ?", (username,))
    new_ids: list[int] = []
    for it in items:
        cur.execute(
            "INSERT INTO custom_expenses (username, category_name, price) VALUES (?, ?, ?)",
            (username, it["cat"], float(it["price"])),
        )
        new_ids.append(cur.lastrowid)
    conn.commit(); conn.close()
    return new_ids


def delete_custom_expense(username: str, expense_id: int) -> None:
    """Удаляет ровно одну пользовательскую категорию по её DB id.
    Проверка username защищает от подмены чужого id."""
    conn = sqlite3.connect(DB_NAME); cur = conn.cursor()
    cur.execute(
        "DELETE FROM custom_expenses WHERE id = ? AND username = ?",
        (int(expense_id), username),
    )
    conn.commit(); conn.close()


def register_user(username, password, region):
    """Регистрирует пользователя в БД"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO users VALUES (?, ?, ?)", (username, password, region))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False  # Пользователь уже существует
    finally:
        conn.close()

def check_login(username, password):
    """Проверяет логин и пароль, возвращает данные пользователя"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT region FROM users WHERE username = ? AND password = ?", (username, password))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else None

def save_calc_data(username, area, density, base_yield, market_price, medicine_price, risk_checked, crop_name):
    """Сохраняет текущие параметры калькулятора для пользователя"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO calculations VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(username) DO UPDATE SET
            area=excluded.area,
            density=excluded.density,
            base_yield=excluded.base_yield,
            market_price=excluded.market_price,
            medicine_price=excluded.medicine_price,
            risk_checked=excluded.risk_checked,
            crop_name=excluded.crop_name
    """, (username, area, density, base_yield, market_price, medicine_price, int(risk_checked), crop_name))
    conn.commit()
    conn.close()

def load_calc_data(username):
    """Загружает сохраненные параметры калькулятора для конкретного пользователя"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT area, density, base_yield, market_price, medicine_price, risk_checked, crop_name FROM calculations WHERE username = ?", (username,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return {
            "area": row[0], "density": row[1], "base_yield": row[2],
            "market_price": row[3], "medicine_price": row[4], "risk_checked": bool(row[5])
        }
    return None

def load_expense_items(username) -> list[dict]:
    conn = sqlite3.connect(DB_NAME); cur = conn.cursor()
    cur.execute(
        "SELECT id, category, name, price, url, source FROM expense_items "
        "WHERE username = ? ORDER BY category, id",
        (username,),
    )
    rows = cur.fetchall(); conn.close()
    return [
        {"id": r[0], "cat": r[1], "name": r[2], "price": r[3], "url": r[4], "source": r[5]}
        for r in rows
    ]


def add_expense_items(username, items: list[dict]) -> None:
    """items = [{cat, name, price, url, source}]. Дубли игнорируются."""
    conn = sqlite3.connect(DB_NAME); cur = conn.cursor()
    for it in items:
        cur.execute(
            "INSERT OR IGNORE INTO expense_items "
            "(username, category, name, price, url, source) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                username, it["cat"], it["name"],
                float(it.get("price") or 0),
                it.get("url") or "",
                it.get("source") or "",
            ),
        )
    conn.commit(); conn.close()


def update_expense_item_price(username, item_id, price) -> None:
    conn = sqlite3.connect(DB_NAME); cur = conn.cursor()
    cur.execute(
        "UPDATE expense_items SET price = ? WHERE id = ? AND username = ?",
        (float(price), int(item_id), username),
    )
    conn.commit(); conn.close()


def delete_expense_item(username, item_id) -> None:
    conn = sqlite3.connect(DB_NAME); cur = conn.cursor()
    cur.execute(
        "DELETE FROM expense_items WHERE id = ? AND username = ?",
        (int(item_id), username),
    )
    conn.commit(); conn.close()


def known_item_names(username, category) -> set[str]:
    conn = sqlite3.connect(DB_NAME); cur = conn.cursor()
    cur.execute(
        "SELECT name FROM expense_items WHERE username = ? AND category = ?",
        (username, category),
    )
    rows = cur.fetchall(); conn.close()
    return {r[0] for r in rows}

def reset_zero_price_items(username: str) -> int:
    """Удаляет позиции с нулевой ценой и без ссылки, чтобы Gemini их перезапросил."""
    conn = sqlite3.connect(DB_NAME); cur = conn.cursor()
    cur.execute(
        "DELETE FROM expense_items "
        "WHERE username = ? AND price = 0 AND url = '' AND source = ''",
        (username,),
    )
    n = cur.rowcount
    conn.commit(); conn.close()
    return n

def update_expense_item_url(username, item_id, url) -> None:
    conn = sqlite3.connect(DB_NAME); cur = conn.cursor()
    cur.execute(
        "UPDATE expense_items SET url = ? WHERE id = ? AND username = ?",
        ((url or "").strip(), int(item_id), username),
    )
    conn.commit(); conn.close()