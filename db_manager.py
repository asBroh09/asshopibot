import aiosqlite
import json
import os
from datetime import datetime, timedelta

DB_FILE = "bot.db"


async def init_db():
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                is_premium INTEGER DEFAULT 0,
                premium_expiry TEXT,
                plan TEXT DEFAULT 'premium'
            )
        """)

        # Migration: Add plan column
        try:
            await db.execute("ALTER TABLE users ADD COLUMN plan TEXT DEFAULT 'premium'")
        except Exception:
            pass

        # Migration: Add custom_limit column
        try:
            await db.execute("ALTER TABLE users ADD COLUMN custom_limit INTEGER DEFAULT NULL")
        except Exception:
            pass

        # Migration: Add is_adm_premium column
        try:
            await db.execute("ALTER TABLE users ADD COLUMN is_adm_premium INTEGER DEFAULT 0")
        except Exception:
            pass

        await db.execute("""
            CREATE TABLE IF NOT EXISTS keys (
                key TEXT PRIMARY KEY,
                type TEXT,
                hours INTEGER,
                expiry TEXT,
                user_limit INTEGER,
                used_count INTEGER,
                used_by TEXT,
                created_at TEXT,
                created_by INTEGER
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS price_filters (
                id INTEGER PRIMARY KEY,
                data TEXT
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS sites_price (
                id INTEGER PRIMARY KEY,
                data TEXT
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS bin_cache (
                bin TEXT PRIMARY KEY,
                brand TEXT,
                type TEXT,
                level TEXT,
                bank TEXT,
                country TEXT,
                flag TEXT
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS check_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                total_cards INTEGER,
                charged INTEGER DEFAULT 0,
                approved INTEGER DEFAULT 0,
                dead INTEGER DEFAULT 0,
                errors INTEGER DEFAULT 0,
                checked_at TEXT
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_sites (
                user_id INTEGER,
                url TEXT,
                price REAL DEFAULT 0.0,
                added_at TEXT,
                PRIMARY KEY (user_id, url)
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_proxies (
                user_id INTEGER,
                proxy TEXT,
                added_at TEXT,
                PRIMARY KEY (user_id, proxy)
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS banned_users (
                user_id INTEGER PRIMARY KEY,
                reason TEXT
            )
        """)

        await db.commit()


async def auto_migrate():
    await init_db()

    async with aiosqlite.connect(DB_FILE) as db:
        # Migrate all_users
        if os.path.exists("all_users.txt"):
            try:
                with open("all_users.txt", "r") as f:
                    users = [int(line.strip()) for line in f if line.strip().isdigit()]
                for uid in users:
                    await db.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (uid,))
                os.rename("all_users.txt", "all_users.txt.bak")
            except Exception as e:
                print("Error migrating all_users.txt:", e)

        # Migrate premium expiry mapping
        expiry_map = {}
        if os.path.exists("premium_expiry.json"):
            try:
                with open("premium_expiry.json", "r") as f:
                    expiry_map = json.load(f)
                os.rename("premium_expiry.json", "premium_expiry.json.bak")
            except Exception as e:
                print("Error migrating premium_expiry.json:", e)

        # Migrate premium_users
        if os.path.exists("premium_users.txt"):
            try:
                with open("premium_users.txt", "r") as f:
                    p_users = [int(line.strip()) for line in f if line.strip().isdigit()]
                for uid in p_users:
                    expiry = expiry_map.get(str(uid), None)
                    await db.execute("""
                        INSERT INTO users (user_id, is_premium, premium_expiry) 
                        VALUES (?, 1, ?) 
                        ON CONFLICT(user_id) DO UPDATE SET is_premium=1, premium_expiry=?
                    """, (uid, expiry, expiry))
                os.rename("premium_users.txt", "premium_users.txt.bak")
            except Exception as e:
                print("Error migrating premium_users.txt:", e)

        # Migrate keys
        if os.path.exists("keys.json"):
            try:
                with open("keys.json", "r") as f:
                    keys = json.load(f)
                for k, v in keys.items():
                    await db.execute("""
                        INSERT OR IGNORE INTO keys 
                        (key, type, hours, expiry, user_limit, used_count, used_by, created_at, created_by)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        k, v.get('type'), v.get('hours'), v.get('expiry'), v.get('user_limit'),
                        v.get('used_count', 0), json.dumps(v.get('used_by', [])),
                        v.get('created_at'), v.get('created_by')
                    ))
                os.rename("keys.json", "keys.json.bak")
            except Exception as e:
                print("Error migrating keys.json:", e)

        # Migrate filters
        if os.path.exists("price_filters.json"):
            try:
                with open("price_filters.json", "r") as f:
                    flt = json.load(f)
                await db.execute("INSERT OR REPLACE INTO price_filters (id, data) VALUES (1, ?)", (json.dumps(flt),))
                os.rename("price_filters.json", "price_filters.json.bak")
            except Exception as e:
                print("Error migrating price_filters.json:", e)

        # Migrate sites price
        if os.path.exists("sites_price.json"):
            try:
                with open("sites_price.json", "r") as f:
                    sp = json.load(f)
                await db.execute("INSERT OR REPLACE INTO sites_price (id, data) VALUES (1, ?)", (json.dumps(sp),))
                os.rename("sites_price.json", "sites_price.json.bak")
            except Exception as e:
                print("Error migrating sites_price.json:", e)

        await db.commit()


# ─── USER DATA ────────────────────────────────────────────────────────

async def get_all_bot_users():
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT user_id FROM users") as cursor:
            rows = await cursor.fetchall()
            return [r[0] for r in rows]


async def save_bot_user(user_id):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (int(user_id),))
        await db.commit()


async def get_premium_users():
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT user_id FROM users WHERE is_premium = 1") as cursor:
            rows = await cursor.fetchall()
            return [str(r[0]) for r in rows]


async def get_premium_users_details():
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT user_id, premium_expiry, plan FROM users WHERE is_premium = 1") as cursor:
            rows = await cursor.fetchall()
            return [{'user_id': r[0], 'expiry': r[1], 'plan': r[2]} for r in rows]


async def get_premium_expiry(user_id):
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT premium_expiry FROM users WHERE user_id = ?", (int(user_id),)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None


async def is_premium_db(user_id, admin_ids):
    if user_id in admin_ids or str(user_id) in [str(a) for a in admin_ids]:
        return True

    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT is_premium, premium_expiry, is_adm_premium FROM users WHERE user_id = ?", (int(user_id),)) as cursor:
            row = await cursor.fetchone()
            if not row:
                return False

            is_prem = row[0]
            expiry_str = row[1]
            is_adm = row[2] if len(row) > 2 and row[2] is not None else 0

            if is_adm == 1 or is_prem == 1:
                if expiry_str:
                    try:
                        exp_time = datetime.fromisoformat(expiry_str)
                        if datetime.now() > exp_time:
                            await db.execute("UPDATE users SET is_premium=0, premium_expiry=NULL, is_adm_premium=0, plan='free' WHERE user_id=?", (int(user_id),))
                            await db.commit()
                            return False
                    except:
                        pass
                return True
            return False


async def get_user_plan(user_id):
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT is_premium, premium_expiry, plan, is_adm_premium FROM users WHERE user_id = ?", (int(user_id),)) as cursor:
            row = await cursor.fetchone()
            if not row:
                return 'free'

            is_prem = row[0]
            expiry_str = row[1]
            plan = row[2]
            is_adm = row[3] if len(row) > 3 and row[3] is not None else 0

            if is_adm == 1 or is_prem == 1:
                if expiry_str:
                    try:
                        exp_time = datetime.fromisoformat(expiry_str)
                        if datetime.now() > exp_time:
                            await db.execute("UPDATE users SET is_premium=0, premium_expiry=NULL, is_adm_premium=0, plan='free' WHERE user_id=?", (int(user_id),))
                            await db.commit()
                            return 'free'
                    except:
                        pass
                return plan if plan else 'premium'
            return 'free'


async def add_premium_user(user_id, expiry=None, plan='premium'):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("""
            INSERT INTO users (user_id, is_premium, premium_expiry, plan) 
            VALUES (?, 1, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET is_premium=1, premium_expiry=?, plan=?
        """, (int(user_id), expiry, plan, expiry, plan))
        await db.commit()


async def remove_premium_user(user_id):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("UPDATE users SET is_premium=0, premium_expiry=NULL, is_adm_premium=0 WHERE user_id=?", (int(user_id),))
        await db.commit()


async def add_adm_premium_user(user_id, expiry=None, plan='premium'):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("""
            INSERT INTO users (user_id, is_premium, premium_expiry, plan, is_adm_premium) 
            VALUES (?, 1, ?, ?, 1)
            ON CONFLICT(user_id) DO UPDATE SET is_premium=1, premium_expiry=?, plan=?, is_adm_premium=1
        """, (int(user_id), expiry, plan, expiry, plan))
        await db.commit()


async def remove_adm_premium_user(user_id):
    async with aiosqlite.connect(DB_FILE) as db:
        cursor = await db.execute("UPDATE users SET is_adm_premium=0 WHERE user_id=?", (int(user_id),))
        await db.commit()
        return cursor.rowcount > 0


async def is_adm_premium(user_id):
    async with aiosqlite.connect(DB_FILE) as db:
        try:
            async with db.execute("SELECT is_adm_premium, is_premium, premium_expiry FROM users WHERE user_id = ?", (int(user_id),)) as cursor:
                row = await cursor.fetchone()
                if not row or row[0] != 1 or row[1] == 0:
                    return False
                expiry_str = row[2]
                if expiry_str:
                    try:
                        exp_time = datetime.fromisoformat(expiry_str)
                        if datetime.now() > exp_time:
                            return False
                    except:
                        pass
                return True
        except Exception:
            return False


async def get_adm_premium_users():
    async with aiosqlite.connect(DB_FILE) as db:
        try:
            async with db.execute("SELECT user_id, premium_expiry, plan FROM users WHERE is_adm_premium = 1 AND is_premium = 1") as cursor:
                rows = await cursor.fetchall()
                return [{'user_id': r[0], 'expiry': r[1], 'plan': r[2]} for r in rows]
        except Exception:
            return []


# ─── BAN SYSTEM ───────────────────────────────────────────────────────

async def ban_user(user_id, reason="Gen Banned"):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS banned_users (
                user_id INTEGER PRIMARY KEY,
                reason TEXT
            )
        """)
        await db.execute("INSERT OR REPLACE INTO banned_users (user_id, reason) VALUES (?, ?)", (int(user_id), reason))
        await db.execute("UPDATE users SET is_premium=0, premium_expiry=NULL WHERE user_id=?", (int(user_id),))
        await db.commit()


async def unban_user(user_id):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS banned_users (
                user_id INTEGER PRIMARY KEY,
                reason TEXT
            )
        """)
        await db.execute("DELETE FROM banned_users WHERE user_id=?", (int(user_id),))
        await db.execute("UPDATE users SET is_premium=0, premium_expiry=NULL, plan='free' WHERE user_id=?", (int(user_id),))
        await db.commit()


async def unban_all_users():
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS banned_users (
                user_id INTEGER PRIMARY KEY,
                reason TEXT
            )
        """)
        banned_ids = []
        try:
            async with db.execute("SELECT user_id FROM banned_users") as cursor:
                async for row in cursor:
                    banned_ids.append(row[0])
        except Exception:
            pass

        await db.execute("DELETE FROM banned_users")
        if banned_ids:
            placeholders = ','.join('?' for _ in banned_ids)
            await db.execute(f"UPDATE users SET is_premium=0, premium_expiry=NULL, plan='free' WHERE user_id IN ({placeholders})", tuple(banned_ids))
        await db.commit()


async def get_banned_users():
    users = []
    async with aiosqlite.connect(DB_FILE) as db:
        try:
            async with db.execute("SELECT user_id, reason FROM banned_users") as cursor:
                async for row in cursor:
                    users.append({'user_id': row[0], 'reason': row[1]})
        except Exception:
            pass
    return users


async def is_banned(user_id):
    async with aiosqlite.connect(DB_FILE) as db:
        try:
            async with db.execute("SELECT 1 FROM banned_users WHERE user_id=?", (int(user_id),)) as cursor:
                row = await cursor.fetchone()
                return bool(row)
        except Exception:
            return False


# ─── KEYS ─────────────────────────────────────────────────────────────

async def load_keys():
    keys = {}
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT * FROM keys") as cursor:
            async for row in cursor:
                k = row[0]
                keys[k] = {
                    'type': row[1],
                    'hours': row[2],
                    'expiry': row[3],
                    'user_limit': row[4],
                    'used_count': row[5],
                    'used_by': json.loads(row[6]) if row[6] else [],
                    'created_at': row[7],
                    'created_by': row[8]
                }
    return keys


async def save_keys(keys_dict):
    async with aiosqlite.connect(DB_FILE) as db:
        for k, v in keys_dict.items():
            await db.execute("""
                INSERT OR REPLACE INTO keys 
                (key, type, hours, expiry, user_limit, used_count, used_by, created_at, created_by)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                k, v.get('type'), v.get('hours'), v.get('expiry'), v.get('user_limit'),
                v.get('used_count', 0), json.dumps(v.get('used_by', [])),
                v.get('created_at'), v.get('created_by')
            ))
        await db.commit()


async def delete_key(key):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("DELETE FROM keys WHERE key=?", (key,))
        await db.commit()


# ─── FILTERS ──────────────────────────────────────────────────────────

async def load_price_filters():
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT data FROM price_filters WHERE id=1") as cursor:
            row = await cursor.fetchone()
            if row:
                return json.loads(row[0])
    return {}


async def save_price_filters(filters_dict):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("INSERT OR REPLACE INTO price_filters (id, data) VALUES (1, ?)", (json.dumps(filters_dict),))
        await db.commit()


async def load_sites_with_price():
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT data FROM sites_price WHERE id=1") as cursor:
            row = await cursor.fetchone()
            if row:
                return json.loads(row[0])
    return []


async def save_sites_with_price(data_list):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("INSERT OR REPLACE INTO sites_price (id, data) VALUES (1, ?)", (json.dumps(data_list),))
        await db.commit()


# ─── BIN CACHE ────────────────────────────────────────────────────────

async def get_cached_bin(bin_number):
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT brand, type, level, bank, country, flag FROM bin_cache WHERE bin=?", (str(bin_number)[:6],)) as cursor:
            row = await cursor.fetchone()
            return row


async def cache_bin(bin_number, brand, btype, level, bank, country, flag):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("""
            INSERT OR IGNORE INTO bin_cache 
            (bin, brand, type, level, bank, country, flag)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (str(bin_number)[:6], brand, btype, level, bank, country, flag))
        await db.commit()


# ─── STATS ────────────────────────────────────────────────────────────

async def get_bot_stats():
    stats = {
        'total_users': 0,
        'premium_users': 0,
        'total_keys': 0,
        'total_bins': 0,
        'total_checks': 0,
        'total_charged': 0,
        'total_approved': 0
    }
    try:
        async with aiosqlite.connect(DB_FILE) as db:
            async with db.execute("SELECT COUNT(*) FROM users") as cursor:
                row = await cursor.fetchone()
                stats['total_users'] = row[0] if row else 0

            async with db.execute("SELECT COUNT(*) FROM users WHERE is_premium = 1") as cursor:
                row = await cursor.fetchone()
                stats['premium_users'] = row[0] if row else 0

            async with db.execute("SELECT COUNT(*) FROM keys") as cursor:
                row = await cursor.fetchone()
                stats['total_keys'] = row[0] if row else 0

            async with db.execute("SELECT COUNT(*) FROM bin_cache") as cursor:
                row = await cursor.fetchone()
                stats['total_bins'] = row[0] if row else 0

            async with db.execute("SELECT COUNT(*), COALESCE(SUM(charged),0), COALESCE(SUM(approved),0) FROM check_history") as cursor:
                row = await cursor.fetchone()
                if row:
                    stats['total_checks'] = row[0]
                    stats['total_charged'] = row[1]
                    stats['total_approved'] = row[2]
    except Exception as e:
        print(f"Error fetching stats: {e}")
    return stats


async def log_check_session(user_id, total_cards, charged, approved, dead, errors):
    try:
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute("""
                INSERT INTO check_history (user_id, total_cards, charged, approved, dead, errors, checked_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (int(user_id), total_cards, charged, approved, dead, errors, datetime.now().isoformat()))
            await db.commit()
    except Exception as e:
        print(f"Error logging check session: {e}")


async def get_user_stats(user_id):
    stats = {
        'total_sessions': 0,
        'total_cards': 0,
        'total_charged': 0,
        'total_approved': 0,
        'total_dead': 0,
        'hit_rate': 0.0,
        'premium_expiry': None,
        'plan': 'free',
        'custom_limit': None
    }
    try:
        async with aiosqlite.connect(DB_FILE) as db:
            async with db.execute("""
                SELECT COUNT(*), COALESCE(SUM(total_cards),0), COALESCE(SUM(charged),0),
                       COALESCE(SUM(approved),0), COALESCE(SUM(dead),0)
                FROM check_history WHERE user_id = ?
            """, (int(user_id),)) as cursor:
                row = await cursor.fetchone()
                if row:
                    stats['total_sessions'] = row[0]
                    stats['total_cards'] = row[1]
                    stats['total_charged'] = row[2]
                    stats['total_approved'] = row[3]
                    stats['total_dead'] = row[4]
                    total_hits = row[2] + row[3]
                    if row[1] > 0:
                        stats['hit_rate'] = round((total_hits / row[1]) * 100, 2)

            async with db.execute("SELECT premium_expiry, plan, is_premium, custom_limit FROM users WHERE user_id = ?", (int(user_id),)) as cursor:
                row = await cursor.fetchone()
                if row and row[2] == 1:
                    stats['premium_expiry'] = row[0]
                    stats['plan'] = row[1] if row[1] else 'premium'
                    stats['custom_limit'] = row[3]
    except Exception as e:
        print(f"Error fetching user stats: {e}")
    return stats


async def add_user_premium_time(user_id, add_delta):
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT premium_expiry FROM users WHERE user_id = ? AND is_premium = 1", (int(user_id),)) as cursor:
            row = await cursor.fetchone()
            if not row:
                return False

            try:
                current_expiry = datetime.fromisoformat(row[0]) if row[0] else datetime.now()
            except:
                current_expiry = datetime.now()

            new_expiry = current_expiry + add_delta
            await db.execute("UPDATE users SET premium_expiry = ? WHERE user_id = ?", (new_expiry.isoformat(), int(user_id)))
            await db.commit()
            return True


async def set_custom_limit(user_id, limit):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("UPDATE users SET custom_limit = ? WHERE user_id = ?", (int(limit), int(user_id)))
        await db.commit()


async def get_expiring_premiums(hours=24):
    users = []
    try:
        now = datetime.now()
        cutoff = now + timedelta(hours=hours)
        async with aiosqlite.connect(DB_FILE) as db:
            async with db.execute("""
                SELECT user_id, premium_expiry FROM users
                WHERE is_premium = 1 AND premium_expiry IS NOT NULL
            """) as cursor:
                async for row in cursor:
                    try:
                        expiry = datetime.fromisoformat(row[1])
                        if now < expiry <= cutoff:
                            users.append({'user_id': row[0], 'expiry': row[1]})
                    except:
                        pass
    except Exception as e:
        print(f"Error fetching expiring premiums: {e}")
    return users


async def revoke_expired_premiums():
    revoked = []
    try:
        now = datetime.now().isoformat()
        async with aiosqlite.connect(DB_FILE) as db:
            async with db.execute("""
                SELECT user_id FROM users
                WHERE is_premium = 1 AND premium_expiry IS NOT NULL AND premium_expiry < ?
            """, (now,)) as cursor:
                async for row in cursor:
                    revoked.append(row[0])
            if revoked:
                await db.execute("""
                    UPDATE users SET is_premium = 0
                    WHERE is_premium = 1 AND premium_expiry IS NOT NULL AND premium_expiry < ?
                """, (now,))
                await db.commit()
    except Exception as e:
        print(f"Error revoking expired premiums: {e}")
    return revoked


# ─── USER SITES (MULTI-TENANT) ───────────────────────────────────────

async def get_user_sites(user_id, min_price=0.0, max_price=30.0):
    sites = []
    try:
        async with aiosqlite.connect(DB_FILE) as db:
            async with db.execute("""
                SELECT url, price FROM user_sites 
                WHERE user_id = ? AND price >= ? AND price <= ?
                ORDER BY added_at DESC
            """, (int(user_id), float(min_price), float(max_price))) as cursor:
                async for row in cursor:
                    sites.append({'url': row[0], 'price': row[1]})
    except Exception as e:
        print(f"Error fetching user sites: {e}")
    return sites


async def get_all_user_sites(user_id):
    sites = []
    try:
        async with aiosqlite.connect(DB_FILE) as db:
            async with db.execute("""
                SELECT url, price FROM user_sites 
                WHERE user_id = ?
                ORDER BY added_at DESC
            """, (int(user_id),)) as cursor:
                async for row in cursor:
                    sites.append({'url': row[0], 'price': row[1]})
    except Exception as e:
        print(f"Error fetching all user sites: {e}")
    return sites


async def add_user_sites(user_id, sites_with_price, max_limit=None):
    """✅ FIXED: Handle max_limit=None (unlimited)"""
    added = 0
    now = datetime.now().isoformat()

    # None → unlimited
    if max_limit is None:
        max_limit = 10 ** 9

    try:
        async with aiosqlite.connect(DB_FILE) as db:
            async with db.execute("SELECT COUNT(*) FROM user_sites WHERE user_id = ?", (int(user_id),)) as cursor:
                row = await cursor.fetchone()
                current_count = row[0] if row else 0

            slots_available = max(0, max_limit - current_count)
            if slots_available <= 0:
                return 0

            for item in sites_with_price[:slots_available]:
                url = item['url'].strip()
                price = float(item.get('price', 0.0))
                try:
                    await db.execute("""
                        INSERT OR REPLACE INTO user_sites (user_id, url, price, added_at)
                        VALUES (?, ?, ?, ?)
                    """, (int(user_id), url, price, now))
                    added += 1
                except Exception:
                    pass
            await db.commit()
    except Exception as e:
        print(f"Error adding user sites: {e}")
    return added


async def remove_user_site(user_id, url):
    try:
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute("DELETE FROM user_sites WHERE user_id = ? AND url = ?", (int(user_id), url.strip()))
            await db.commit()
            return True
    except Exception as e:
        print(f"Error removing user site: {e}")
        return False


async def clear_user_sites(user_id):
    try:
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute("DELETE FROM user_sites WHERE user_id = ?", (int(user_id),))
            await db.commit()
            return True
    except Exception as e:
        print(f"Error clearing user sites: {e}")
        return False


async def count_user_sites(user_id):
    try:
        async with aiosqlite.connect(DB_FILE) as db:
            async with db.execute("SELECT COUNT(*) FROM user_sites WHERE user_id = ?", (int(user_id),)) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else 0
    except Exception:
        return 0


# ─── USER PROXIES (MULTI-TENANT) ─────────────────────────────────────

async def get_user_proxies(user_id):
    proxies = []
    try:
        async with aiosqlite.connect(DB_FILE) as db:
            async with db.execute("""
                SELECT proxy FROM user_proxies 
                WHERE user_id = ?
                ORDER BY added_at DESC
            """, (int(user_id),)) as cursor:
                async for row in cursor:
                    proxies.append(row[0])
    except Exception as e:
        print(f"Error fetching user proxies: {e}")
    return proxies


async def add_user_proxies(user_id, proxy_list, max_limit=None):
    """✅ FIXED: Handle max_limit=None (unlimited)"""
    added = 0
    now = datetime.now().isoformat()

    # None → unlimited
    if max_limit is None:
        max_limit = 10 ** 9

    try:
        async with aiosqlite.connect(DB_FILE) as db:
            async with db.execute("SELECT COUNT(*) FROM user_proxies WHERE user_id = ?", (int(user_id),)) as cursor:
                row = await cursor.fetchone()
                current_count = row[0] if row else 0

            slots_available = max(0, max_limit - current_count)
            if slots_available <= 0:
                return 0

            for p in proxy_list[:slots_available]:
                p_clean = p.strip()
                if not p_clean:
                    continue
                try:
                    await db.execute("""
                        INSERT OR REPLACE INTO user_proxies (user_id, proxy, added_at)
                        VALUES (?, ?, ?)
                    """, (int(user_id), p_clean, now))
                    added += 1
                except Exception:
                    pass
            await db.commit()
    except Exception as e:
        print(f"Error adding user proxies: {e}")
    return added


async def remove_user_proxy(user_id, proxy):
    try:
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute("DELETE FROM user_proxies WHERE user_id = ? AND proxy = ?", (int(user_id), proxy.strip()))
            await db.commit()
            return True
    except Exception as e:
        print(f"Error removing user proxy: {e}")
        return False


async def clear_user_proxies(user_id):
    try:
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute("DELETE FROM user_proxies WHERE user_id = ?", (int(user_id),))
            await db.commit()
            return True
    except Exception as e:
        print(f"Error clearing user proxies: {e}")
        return False


async def count_user_proxies(user_id):
    try:
        async with aiosqlite.connect(DB_FILE) as db:
            async with db.execute("SELECT COUNT(*) FROM user_proxies WHERE user_id = ?", (int(user_id),)) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else 0
    except Exception:
        return 0


# ─── DETAILED USER INFO ──────────────────────────────────────────────

async def get_all_users_detailed():
    users_dict = {}
    async with aiosqlite.connect(DB_FILE) as db:
        # 1. Get all bot users
        try:
            async with db.execute("SELECT user_id FROM users") as cursor:
                async for row in cursor:
                    uid = row[0]
                    users_dict[uid] = {
                        'user_id': uid,
                        'joined_at': 'N/A',
                        'plan': 'free',
                        'is_premium': 0,
                        'premium_expiry': None,
                        'custom_limit': None,
                        'sites_count': 0,
                        'proxies_count': 0,
                        'total_cards': 0,
                        'charged': 0,
                        'approved': 0,
                        'dead': 0,
                        'sessions': 0,
                        'hit_rate': 0.0,
                        'is_banned': False,
                        'ban_reason': ''
                    }
        except Exception:
            pass

        # 2. Get users table data
        try:
            async with db.execute("SELECT user_id, is_premium, premium_expiry, plan, custom_limit FROM users") as cursor:
                async for row in cursor:
                    uid = row[0]
                    if uid not in users_dict:
                        users_dict[uid] = {
                            'user_id': uid, 'joined_at': 'N/A', 'sites_count': 0, 'proxies_count': 0,
                            'total_cards': 0, 'charged': 0, 'approved': 0, 'dead': 0, 'sessions': 0,
                            'hit_rate': 0.0, 'is_banned': False, 'ban_reason': ''
                        }
                    is_prem = row[1]
                    expiry_str = row[2]
                    plan = row[3] if row[3] else 'free'
                    if is_prem and expiry_str:
                        try:
                            if datetime.now() > datetime.fromisoformat(expiry_str):
                                is_prem = 0
                                plan = 'free'
                        except:
                            pass
                    users_dict[uid]['is_premium'] = is_prem
                    users_dict[uid]['premium_expiry'] = expiry_str
                    users_dict[uid]['plan'] = plan if is_prem else 'free'
                    users_dict[uid]['custom_limit'] = row[4]
        except Exception:
            pass

        # 3. Get site counts
        try:
            async with db.execute("SELECT user_id, COUNT(*) FROM user_sites GROUP BY user_id") as cursor:
                async for row in cursor:
                    uid = row[0]
                    if uid in users_dict:
                        users_dict[uid]['sites_count'] = row[1]
        except Exception:
            pass

        # 4. Get proxy counts
        try:
            async with db.execute("SELECT user_id, COUNT(*) FROM user_proxies GROUP BY user_id") as cursor:
                async for row in cursor:
                    uid = row[0]
                    if uid in users_dict:
                        users_dict[uid]['proxies_count'] = row[1]
        except Exception:
            pass

        # 5. Get check history stats
        try:
            async with db.execute("""
                SELECT user_id, COUNT(*), COALESCE(SUM(total_cards),0), COALESCE(SUM(charged),0),
                       COALESCE(SUM(approved),0), COALESCE(SUM(dead),0)
                FROM check_history GROUP BY user_id
            """) as cursor:
                async for row in cursor:
                    uid = row[0]
                    if uid in users_dict:
                        u = users_dict[uid]
                        u['sessions'] = row[1]
                        u['total_cards'] = row[2]
                        u['charged'] = row[3]
                        u['approved'] = row[4]
                        u['dead'] = row[5]
                        total_hits = row[3] + row[4]
                        if row[2] > 0:
                            u['hit_rate'] = round((total_hits / row[2]) * 100, 2)
        except Exception:
            pass

        # 6. Get banned users
        try:
            async with db.execute("SELECT user_id, reason FROM banned_users") as cursor:
                async for row in cursor:
                    uid = row[0]
                    if uid in users_dict:
                        users_dict[uid]['is_banned'] = True
                        users_dict[uid]['ban_reason'] = row[1]
        except Exception:
            pass

    return list(users_dict.values())


async def get_all_sites_database():
    results = []
    async with aiosqlite.connect(DB_FILE) as db:
        try:
            async with db.execute("SELECT user_id, url, price, added_at FROM user_sites ORDER BY added_at DESC") as cursor:
                async for row in cursor:
                    results.append({'user_id': row[0], 'url': row[1], 'price': row[2], 'added_at': row[3]})
        except Exception:
            pass
    return results


async def get_all_proxies_database():
    results = []
    async with aiosqlite.connect(DB_FILE) as db:
        try:
            async with db.execute("SELECT user_id, proxy, added_at FROM user_proxies ORDER BY added_at DESC") as cursor:
                async for row in cursor:
                    results.append({'user_id': row[0], 'proxy': row[1], 'added_at': row[2]})
        except Exception:
            pass
    return results


async def get_single_user_detailed_info(user_id):
    info = {
        'user_id': int(user_id),
        'plan': 'free',
        'is_premium': 0,
        'premium_expiry': None,
        'custom_limit': None,
        'sites_count': 0,
        'proxies_count': 0,
        'total_cards': 0,
        'charged': 0,
        'approved': 0,
        'dead': 0,
        'sessions': 0,
        'hit_rate': 0.0,
        'is_banned': False,
        'ban_reason': ''
    }
    async with aiosqlite.connect(DB_FILE) as db:
        try:
            async with db.execute("SELECT is_premium, premium_expiry, plan, custom_limit FROM users WHERE user_id = ?", (int(user_id),)) as cursor:
                row = await cursor.fetchone()
                if row:
                    is_prem = row[0]
                    expiry_str = row[1]
                    plan = row[2] if row[2] else 'free'
                    if is_prem and expiry_str:
                        try:
                            if datetime.now() > datetime.fromisoformat(expiry_str):
                                is_prem = 0
                                plan = 'free'
                        except:
                            pass
                    info['is_premium'] = is_prem
                    info['premium_expiry'] = expiry_str
                    info['plan'] = plan if is_prem else 'free'
                    info['custom_limit'] = row[3]
        except Exception:
            pass

        try:
            async with db.execute("SELECT COUNT(*) FROM user_sites WHERE user_id = ?", (int(user_id),)) as cursor:
                row = await cursor.fetchone()
                info['sites_count'] = row[0] if row else 0
        except Exception:
            pass

        try:
            async with db.execute("SELECT COUNT(*) FROM user_proxies WHERE user_id = ?", (int(user_id),)) as cursor:
                row = await cursor.fetchone()
                info['proxies_count'] = row[0] if row else 0
        except Exception:
            pass

        try:
            async with db.execute("""
                SELECT COUNT(*), COALESCE(SUM(total_cards),0), COALESCE(SUM(charged),0),
                       COALESCE(SUM(approved),0), COALESCE(SUM(dead),0)
                FROM check_history WHERE user_id = ?
            """, (int(user_id),)) as cursor:
                row = await cursor.fetchone()
                if row:
                    info['sessions'] = row[0]
                    info['total_cards'] = row[1]
                    info['charged'] = row[2]
                    info['approved'] = row[3]
                    info['dead'] = row[4]
                    total_hits = row[2] + row[3]
                    if row[1] > 0:
                        info['hit_rate'] = round((total_hits / row[1]) * 100, 2)
        except Exception:
            pass

        try:
            async with db.execute("SELECT reason FROM banned_users WHERE user_id = ?", (int(user_id),)) as cursor:
                row = await cursor.fetchone()
                if row:
                    info['is_banned'] = True
                    info['ban_reason'] = row[0]
        except Exception:
            pass

    return info
