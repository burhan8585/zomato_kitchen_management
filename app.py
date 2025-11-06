import sqlite3
from flask import Flask, request, render_template, redirect, url_for, g, session, flash, make_response
from functools import wraps

app = Flask(__name__)
app.secret_key = 'your_secret_key_here'  # Change this to a more secure key in production
# DATABASE_URL = "postgresql://username:password@localhost:5432/zomato_kitchen"

db_initialized = False  # Global flag for DB init

# ========================================
# Auth
# ========================================
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'admin_logged_in' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username', '')
        password = request.form.get('password', '')
        if username == 'admin' and password == 'admin123':
            session['admin_logged_in'] = True
            flash("Welcome back, admin!", "success")
            return redirect(url_for('index'))
        flash("Invalid credentials, try again.", "danger")
        return redirect(url_for('login'))
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    session.pop('admin_logged_in', None)
    flash("Logged out successfully.", "info")
    return redirect(url_for('login'))

# ========================================
# DB Management
# ========================================
def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect('zomato_kitchen.db')
        g.db.row_factory = sqlite3.Row
    return g.db

def initialize_database():
    conn = get_db()
    cur = conn.cursor()
    cur.executescript("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_date TEXT NOT NULL,
            total_amount REAL NOT NULL,
            zomato_commission REAL NOT NULL,
            net_income REAL NOT NULL,
            ingredient_total REAL NOT NULL,
            cutlery_cost REAL NOT NULL,
            profit_loss REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            item_name TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            total_item_cost REAL NOT NULL,
            FOREIGN KEY (order_id) REFERENCES orders(id)
        );

        CREATE TABLE IF NOT EXISTS ingredients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id INTEGER NOT NULL,
            ingredient_name TEXT NOT NULL,
            ingredient_cost REAL NOT NULL,
            FOREIGN KEY (item_id) REFERENCES items(id)
        );

        CREATE TABLE IF NOT EXISTS stock (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ingredient_name TEXT UNIQUE NOT NULL,
            quantity REAL NOT NULL,
            unit TEXT NOT NULL,
            min_threshold REAL DEFAULT 0
        );
    """)
    conn.commit()

def migrate_stock_table():
    """
    Ensure stock.ingredient_name has a UNIQUE constraint and fix older tables.
    """
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='stock'")
    if not cur.fetchone():
        return  # Fresh DB, stock already created in initialize_database()

    cur.execute("PRAGMA index_list('stock')")
    unique_index_found = any(idx[2] == 1 for idx in cur.fetchall())
    if unique_index_found:
        return  # Already has UNIQUE constraint

    cur.execute("ALTER TABLE stock RENAME TO stock_old;")
    cur.executescript("""
        CREATE TABLE stock (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ingredient_name TEXT UNIQUE NOT NULL,
            quantity REAL NOT NULL,
            unit TEXT NOT NULL,
            min_threshold REAL DEFAULT 0
        );
        INSERT INTO stock (ingredient_name, quantity, unit, min_threshold)
        SELECT ingredient_name, SUM(quantity), unit, MIN(min_threshold)
        FROM stock_old
        GROUP BY ingredient_name;
        DROP TABLE stock_old;
    """)
    conn.commit()

def migrate_database():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(orders)")
    order_cols = [c[1] for c in cur.fetchall()]
    if 'ingredient_total' not in order_cols:
        cur.execute("ALTER TABLE orders ADD COLUMN ingredient_total REAL DEFAULT 0")
    if 'cutlery_cost' not in order_cols:
        cur.execute("ALTER TABLE orders ADD COLUMN cutlery_cost REAL DEFAULT 0")
    migrate_stock_table()
    conn.commit()

@app.before_request
def before_request_func():
    global db_initialized
    if not db_initialized:
        initialize_database()
        migrate_database()
        db_initialized = True

@app.teardown_appcontext
def close_db(exception):
    db = g.pop('db', None)
    if db is not None:
        db.close()

# ========================================
# Utilities
# ========================================
def parse_items_from_form(form):
    items = {}
    for key in form:
        if key.startswith("item_name_"):
            item_id = int(key.split("_")[2])
            items[item_id] = {
                "name": form[key],
                "quantity": int(form.get(f"quantity_{item_id}", 0)),
                "ingredients": []
            }
    for key in form:
        if key.startswith("ingredient_name_"):
            _, _, item_id_str, ing_idx = key.split("_")
            item_id = int(item_id_str)
            ing_name = form[key]
            ing_cost = float(form.get(f"ingredient_cost_{item_id}_{ing_idx}", 0))
            items[item_id]["ingredients"].append({"name": ing_name, "cost": ing_cost})
    return items

def deduct_stock_for_ingredient(cur, ingredient_name, qty_to_deduct):
    cur.execute("UPDATE stock SET quantity = MAX(0, quantity - ?) WHERE ingredient_name = ?", (qty_to_deduct, ingredient_name))

# ========================================
# Main Routes
# ========================================
@app.route('/')
@login_required
def index():
    conn = get_db()
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')
    query = "SELECT * FROM orders ORDER BY date(order_date) DESC"
    params = ()
    if start_date and end_date:
        query = "SELECT * FROM orders WHERE order_date BETWEEN ? AND ? ORDER BY date(order_date) DESC"
        params = (start_date, end_date)
    orders = conn.execute(query, params).fetchall()

    low_stock_items = conn.execute("""
        SELECT ingredient_name, quantity, unit
        FROM stock WHERE quantity <= min_threshold ORDER BY ingredient_name
    """).fetchall()
    return render_template('index.html', orders=orders, low_stock_items=low_stock_items)

@app.route('/add', methods=['GET', 'POST'])
@login_required
def add_order():
    if request.method == 'POST':
        conn = get_db()
        cur = conn.cursor()
        order_date = request.form['date']
        total_amount = float(request.form['total_amount'])
        cutlery_cost = float(request.form.get('cutlery_cost', 0))
        zomato_commission = total_amount * 0.22
        net_income = total_amount - zomato_commission
        cur.execute("""
            INSERT INTO orders (order_date, total_amount, zomato_commission, net_income, ingredient_total, cutlery_cost, profit_loss)
            VALUES (?, ?, ?, ?, 0, ?, 0)
        """, (order_date, total_amount, zomato_commission, net_income, cutlery_cost))
        order_id = cur.lastrowid

        items = parse_items_from_form(request.form)
        total_ingredient_cost = 0.0
        for item in items.values():
            item_name = item['name']
            quantity = item['quantity']
            ingredient_cost_total = sum(i['cost'] for i in item['ingredients'])
            cur.execute("""
                INSERT INTO items (order_id, item_name, quantity, total_item_cost)
                VALUES (?, ?, ?, ?)
            """, (order_id, item_name, quantity, ingredient_cost_total * quantity))
            item_id = cur.lastrowid
            for ing in item['ingredients']:
                cur.execute("INSERT INTO ingredients (item_id, ingredient_name, ingredient_cost) VALUES (?, ?, ?)", 
                            (item_id, ing['name'], ing['cost']))
            total_ingredient_cost += ingredient_cost_total * quantity

        profit_loss = net_income - (total_ingredient_cost + cutlery_cost)
        cur.execute("UPDATE orders SET ingredient_total = ?, profit_loss = ? WHERE id = ?", 
                    (total_ingredient_cost, profit_loss, order_id))
        conn.commit()
        flash("Order added successfully!", "success")
        return redirect(url_for('index'))
    return render_template('add_order.html')

@app.route('/edit/<int:order_id>', methods=['GET', 'POST'])
@login_required
def edit_order(order_id):
    conn = get_db()
    cur = conn.cursor()
    if request.method == 'POST':
        cur.execute('DELETE FROM ingredients WHERE item_id IN (SELECT id FROM items WHERE order_id=?)', (order_id,))
        cur.execute('DELETE FROM items WHERE order_id=?', (order_id,))
        order_date = request.form['date']
        total_amount = float(request.form['total_amount'])
        cutlery_cost = float(request.form.get('cutlery_cost', 0))
        zomato_commission = total_amount * 0.22
        net_income = total_amount - zomato_commission
        items = parse_items_from_form(request.form)
        total_ingredient_cost = 0.0
        for item in items.values():
            item_name = item['name']
            quantity = item['quantity']
            ingredient_cost_total = sum(i['cost'] for i in item['ingredients'])
            cur.execute("""
                INSERT INTO items (order_id, item_name, quantity, total_item_cost)
                VALUES (?, ?, ?, ?)
            """, (order_id, item_name, quantity, ingredient_cost_total * quantity))
            item_id = cur.lastrowid
            for ing in item['ingredients']:
                cur.execute("INSERT INTO ingredients (item_id, ingredient_name, ingredient_cost) VALUES (?, ?, ?)", 
                            (item_id, ing['name'], ing['cost']))
            total_ingredient_cost += ingredient_cost_total * quantity

        profit_loss = net_income - (total_ingredient_cost + cutlery_cost)
        cur.execute("""
            UPDATE orders SET order_date=?, total_amount=?, zomato_commission=?, net_income=?, ingredient_total=?, cutlery_cost=?, profit_loss=? WHERE id=?
        """, (order_date, total_amount, zomato_commission, net_income, total_ingredient_cost, cutlery_cost, profit_loss, order_id))
        conn.commit()
        flash("Order updated successfully!", "success")
        return redirect(url_for('view_order', order_id=order_id))

    order = conn.execute('SELECT * FROM orders WHERE id = ?', (order_id,)).fetchone()
    items = conn.execute('SELECT * FROM items WHERE order_id = ?', (order_id,)).fetchall()
    item_details = [{'item': it, 'ingredients': conn.execute('SELECT * FROM ingredients WHERE item_id = ?', (it['id'],)).fetchall()} for it in items]
    return render_template('edit_order.html', order=order, item_details=item_details)

@app.route('/order/<int:order_id>')
@login_required
def view_order(order_id):
    conn = get_db()
    order = conn.execute('SELECT * FROM orders WHERE id = ?', (order_id,)).fetchone()
    items = conn.execute('SELECT * FROM items WHERE order_id = ?', (order_id,)).fetchall()
    item_details = [{'item': it, 'ingredients': conn.execute('SELECT * FROM ingredients WHERE item_id = ?', (it['id'],)).fetchall()} for it in items]
    return render_template('view_order.html', order=order, item_details=item_details)

@app.route('/delete/<int:order_id>')
@login_required
def delete_order(order_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute('DELETE FROM ingredients WHERE item_id IN (SELECT id FROM items WHERE order_id=?)', (order_id,))
    cur.execute('DELETE FROM items WHERE order_id=?', (order_id,))
    cur.execute('DELETE FROM orders WHERE id=?', (order_id,))
    conn.commit()
    flash("Order deleted.", "info")
    return redirect(url_for('index'))


@app.route('/export/csv')
@login_required
def export_csv():
    conn = get_db()
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')

    if start_date and end_date:
        query = """
            SELECT * FROM orders
            WHERE order_date BETWEEN ? AND ?
            ORDER BY date(order_date) DESC
        """
        orders = conn.execute(query, (start_date, end_date)).fetchall()
    else:
        orders = conn.execute("SELECT * FROM orders ORDER BY date(order_date) DESC").fetchall()

    # Create CSV content
    csv_content = "Date,Total Amount,Commission,Net Income,Ingredient Cost,Cutlery Cost,Profit/Loss\n"
    for order in orders:
        csv_content += f"{order['order_date']},{order['total_amount']},{order['zomato_commission']},{order['net_income']},{order['ingredient_total']},{order['cutlery_cost']},{order['profit_loss']}\n"

    response = make_response(csv_content)
    response.headers['Content-Disposition'] = 'attachment; filename=orders_export.csv'
    response.mimetype = 'text/csv'
    return response

@app.route('/summary')
@login_required
def summary():
    conn = get_db()
    cur = conn.cursor()
    total_revenue = cur.execute("SELECT SUM(total_amount) FROM orders").fetchone()[0] or 0
    total_commission = cur.execute("SELECT SUM(zomato_commission) FROM orders").fetchone()[0] or 0
    total_ingredient_cost = cur.execute("SELECT SUM(ingredient_total) FROM orders").fetchone()[0] or 0
    total_cutlery_cost = cur.execute("SELECT SUM(cutlery_cost) FROM orders").fetchone()[0] or 0
    total_profit_loss = cur.execute("SELECT SUM(profit_loss) FROM orders").fetchone()[0] or 0
    top_items = cur.execute("""
        SELECT item_name, SUM(quantity) AS total_qty FROM items
        GROUP BY item_name ORDER BY total_qty DESC LIMIT 5
    """).fetchall()
    return render_template('summary.html',
                           total_revenue=total_revenue,
                           total_commission=total_commission,
                           total_ingredient_cost=total_ingredient_cost,
                           total_cutlery_cost=total_cutlery_cost,
                           total_profit_loss=total_profit_loss,
                           top_items=top_items)

# ========================================
# Stock Management
# ========================================
@app.route('/stock')
@login_required
def stock():
    conn = get_db()
    stocks = conn.execute("SELECT * FROM stock ORDER BY ingredient_name").fetchall()
    return render_template('stock.html', stocks=stocks)

@app.route('/stock/add', methods=['GET', 'POST'])
@login_required
def add_stock():
    if request.method == 'POST':
        ingredient_name = request.form['ingredient_name']
        quantity = float(request.form['quantity'])
        unit = request.form['unit']
        min_threshold = float(request.form.get('min_threshold', 0))
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO stock (ingredient_name, quantity, unit, min_threshold)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(ingredient_name) DO UPDATE SET
                quantity = quantity + EXCLUDED.quantity,
                unit = EXCLUDED.unit,
                min_threshold = EXCLUDED.min_threshold
        """, (ingredient_name, quantity, unit, min_threshold))
        conn.commit()
        flash(f"{ingredient_name} stock updated!", "success")
        return redirect(url_for('stock'))
    return render_template('add_stock.html')

@app.route('/stock/update/<int:stock_id>', methods=['POST'])
@login_required
def update_stock(stock_id):
    quantity = float(request.form.get('quantity', 0))
    unit = request.form.get('unit', '').strip()
    min_threshold = float(request.form.get('min_threshold', 0))
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        UPDATE stock SET quantity=?, unit=?, min_threshold=? WHERE id=?
    """, (quantity, unit, min_threshold, stock_id))
    conn.commit()
    flash("Stock updated.", "success")
    return redirect(url_for('stock'))

@app.route('/stock/delete/<int:stock_id>', methods=['POST'])
@login_required
def delete_stock(stock_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM stock WHERE id=?", (stock_id,))
    conn.commit()
    flash("Stock item removed.", "info")
    return redirect(url_for('stock'))

#excel 




# ========================================
# Run App
# ========================================
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)

