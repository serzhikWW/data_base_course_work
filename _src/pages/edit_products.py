import asyncio
import asyncpg
import streamlit as st
from contextlib import asynccontextmanager
import pandas as pd
import time


import redis
import pickle
from datetime import timedelta

# Инициализация Redis
redis_client = redis.Redis(
    host='localhost',
    port=6379,
    db=0,
    decode_responses=False
)

def get_user_role_from_cache(user_id):
    """Получаем роль пользователя из кеша Redis"""
    try:
        cached_data = redis_client.get(f"user:{user_id}")
        if cached_data:
            return pickle.loads(cached_data)['role']
        return None
    except:
        return None

def cache_user_data(user_id, user_data, ttl=3600):
    """Кешируем данные пользователя"""
    try:
        redis_client.setex(
            f"user:{user_id}",
            timedelta(seconds=ttl),
            pickle.dumps(user_data))
    except Exception as e:
        print(f"Redis cache error: {e}")


# Асинхронный контекст для подключения к базе данных
@asynccontextmanager
async def get_connection():
    conn = None
    try:
        conn = await asyncpg.connect(
            host="localhost",
            port=5432,
            database="postgres",
            user="postgres",
            password="password"
        )
        yield conn
    finally:
        if conn:
            await conn.close()


async def check_customer_with_cache(id):
    """Проверка пользователя с кешированием"""
    # Сначала проверяем кеш
    cached_role = get_user_role_from_cache(id)
    if cached_role:
        return {'role': cached_role}

    # Если нет в кеше, запрашиваем из БД
    async with get_connection() as conn:
        query = '''SELECT customers.*, roles.role 
                   FROM customers 
                   JOIN roles ON customers.role = roles.id
                   WHERE customers.id = $1'''
        user_data = await conn.fetchrow(query, id)
        if user_data:
            cache_user_data(id, dict(user_data))
        return user_data


# Асинхронная функция для получения продуктов в формате DataFrame
async def get_products_dataframe():
    async with get_connection() as conn:
        query = '''
            SELECT p.id AS product_id,
                   p.name AS product_name,
                   description,
                   price,
                   stock_quantity,
                   b.brand_name AS brand_name,
                   c.name AS category_name
            FROM products p
            JOIN brands b ON p.brand_id = b.id
            JOIN categories c ON p.category_id = c.id
            ORDER BY product_name
        '''
        rows = await conn.fetch(query)
        df = pd.DataFrame([dict(row) for row in rows])
        return df


# Асинхронная операция добавления, удаления или изменения через DataFrame
async def sync_dataframe_changes(df: pd.DataFrame, original_ids: list):
    async with get_connection() as conn:
        for index, row in df.iterrows():
            product_id = row['product_id']

            # Если запись существует в базе данных, обновляем ее, иначе добавляем
            if product_id in original_ids:
                query = '''
                    UPDATE products
                    SET name = $1, description = $2, price = $3, stock_quantity = $4
                    WHERE id = $5
                '''
                await conn.execute(query, row['product_name'], row['description'], row['price'], row['stock_quantity'],
                                   product_id)
            else:
                category_id = await find_category_id(conn, row['category_name'])
                brand_id = await find_brand_id(conn, row['brand_name'])
                query = '''
                    INSERT INTO products (name, description, price, stock_quantity, category_id, brand_id)
                    VALUES ($1, $2, $3, $4, $5, $6)
                '''
                await conn.execute(query, row['product_name'], row['description'], row['price'], row['stock_quantity'],
                                   category_id, brand_id)

        # Удаление записей, которые отсутствуют в текущей версии DataFrame
        ids_to_delete = list(set(original_ids) - set(df['product_id']))
        if ids_to_delete:
            await conn.execute("DELETE FROM order_items WHERE product_id = ANY($1)", ids_to_delete)
            await conn.execute("DELETE FROM products WHERE id = ANY($1)", ids_to_delete)


async def find_category_id(conn, category_name):
    query = "SELECT id FROM categories WHERE name = $1"
    category_id = await conn.fetchval(query, category_name)

    if not category_id:
        query_insert = "INSERT INTO categories (name) VALUES ($1) RETURNING id"
        category_id = await conn.fetchval(query_insert, category_name)

    return category_id


async def find_brand_id(conn, brand_name):
    query = "SELECT id FROM brands WHERE brand_name = $1"
    brand_id = await conn.fetchval(query, brand_name)

    if not brand_id:
        query_insert = "INSERT INTO brands (brand_name) VALUES ($1) RETURNING id"
        brand_id = await conn.fetchval(query_insert, brand_name)

    return brand_id


def products_management_page():
    st.title('Управление товарами в базе данных')

    # Получаем данные пользователя с кешированием
    try:
        user_data = asyncio.run(check_customer_with_cache(st.session_state.user_id))
        user_role = user_data.get('role')

        if user_role == 'customer':  # Предполагаем, что есть роль 'admin'
            st.error("Доступ запрещен: недостаточно прав")
            time.sleep(2)
            return

    except Exception as e:
        st.error(f"Ошибка загрузки данных: {str(e)}")
        st.stop()

    # Основной функционал для администраторов
    st.subheader("Интерактивный список товаров")

    # Загрузка данных с индикатором прогресса
    progress_bar = st.progress(0)
    status_text = st.empty()

    try:
        status_text.text("Загрузка данных...")
        products_df = asyncio.run(get_products_dataframe())
        progress_bar.progress(50)

        if not products_df.empty:
            original_ids = products_df['product_id'].tolist()

            status_text.text("Подготовка интерфейса...")
            edited_df = st.data_editor(
                products_df,
                use_container_width=True,
                num_rows="dynamic",
                column_config={
                    "product_id": st.column_config.NumberColumn("ID", disabled=True),
                    "price": st.column_config.NumberColumn("Цена", format="%.2f ₽"),
                    "stock_quantity": st.column_config.NumberColumn("Остаток", format="%d шт")
                },
                key="products_editor"
            )
            progress_bar.progress(75)

            if st.button("💾 Сохранить изменения", type="primary"):
                if sum(edited_df[edited_df.columns[1:]].isna().any(axis=1)) > 0:
                    st.error("Все поля должны быть заполнены!")
                    return

                try:
                    with st.spinner("Сохранение изменений..."):
                        asyncio.run(sync_dataframe_changes(edited_df, original_ids))
                        # Очищаем кеш товаров после изменений
                        redis_client.delete("products:data")
                    st.success("Изменения успешно сохранены!")
                    time.sleep(1)
                    st.rerun()
                except Exception as e:
                    st.error(f"Ошибка сохранения: {str(e)}")
        else:
            st.info("📦 В базе данных пока нет товаров. Добавьте их через таблицу выше.")

        progress_bar.progress(100)
        status_text.text("Готово!")
        time.sleep(0.5)
        progress_bar.empty()
        status_text.empty()

    except Exception as e:
        st.error(f"Ошибка загрузки товаров: {str(e)}")
        st.stop()