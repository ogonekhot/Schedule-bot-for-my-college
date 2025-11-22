import aiomysql

async def connect():
    conn = await aiomysql.connect(
        host='*****',
        user='*****',
        password='*****',
        db='*****'
    )

    return conn

async def check_tg_id(tg_id):
    conn = await connect()
    
    async with conn.cursor() as cur:
        await cur.execute("SELECT id FROM users WHERE tg_id=%s", (tg_id,))
        db_user_id = await cur.fetchone()

    if db_user_id is None:
        async with conn.cursor() as cur:
            await cur.execute("INSERT INTO users (tg_id) VALUES (%s)", (tg_id,))
            await conn.commit()

        async with conn.cursor() as cur:
            await cur.execute("SELECT id FROM users WHERE tg_id=%s", (tg_id,))
            db_user_id = await cur.fetchone()
    
    async with conn.cursor() as cur:
        await cur.execute("SELECT * FROM settings WHERE id=%s", (db_user_id[0],))
        user_id = await cur.fetchone()

    if user_id is None:
        async with conn.cursor() as cur:
            await cur.execute("INSERT INTO settings (id) VALUES (%s)", (db_user_id[0],))
            await conn.commit()
        
        async with conn.cursor() as cur:
            await cur.execute("SELECT * FROM settings WHERE id=%s", (db_user_id[0],))
            user_id = await cur.fetchone()

    return list(user_id)
