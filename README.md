# Smart Inventory and Retail DSS

A comprehensive Django-based inventory management and decision support system for retail operations.

## 📋 Project Overview

Smart Inventory and Retail DSS is a powerful web application designed to streamline inventory management, track purchases and sales, manage suppliers, and provide data-driven insights for retail operations. Built with Django framework and modern data analysis tools.

## ✨ Features

- **Product Management** - Track and manage product inventory
- **Supplier Management** - Manage supplier information and relationships
- **Purchase Tracking** - Record and monitor purchase orders
- **Sales Management** - Track sales transactions and revenue
- **Inventory Control** - Real-time inventory monitoring and alerts
- **User Management** - Role-based access control for different users
- **Data Analytics** - Generate reports and insights using pandas and numpy
- **Excel Export** - Export data to Excel format using openpyxl

## 🛠 Tech Stack

- **Backend**: Django 4.2.11
- **API Framework**: Django REST Framework 3.14.0
- **Database**: PostgreSQL 15
- **Data Analysis**: Pandas 2.0.3, NumPy 1.26.4
- **Excel Processing**: openpyxl 3.1.2

## 📁 Project Structure

```
SMART_INVENTORY_AND_RETAIL_DSS/
├── manage.py                 # Django management script
├── requirements.txt          # Python dependencies
├├── .gitignore              # Git ignore file
├── Project Proposal-final.pdf  # Project proposal document
├── smart_inventory/        # Main Django project
│   ├── __init__.py
│   ├── settings.py         # Django settings
│   ├── urls.py             # URL routing
│   ├── wsgi.py             # WSGI configuration
│   └── asgi.py             # ASGI configuration
├── inventory/              # Inventory management app
├── products/               # Product management app
├── purchases/              # Purchase tracking app
├── sales/                  # Sales management app
├── suppliers/              # Supplier management app
└── users/                  # User management app
```

## 🚀 Getting Started

### Prerequisites

- Python 3.8 or higher
- PostgreSQL 15
- pip or pipenv
- Git

### Installation

1. **Clone the repository**
   
```
bash
   git clone <https://github.com/Randika2002-Udayangani/SMART_INVENTORY_AND_RETAIL_DSS.git>
   cd SMART_INVENTORY_AND_RETAIL_DSS
   
```

2. **Create a virtual environment** (optional but recommended)
   
```
bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   
```

3. **Install dependencies**
   
```
bash
   pip install -r requirements.txt
   
```

4. **Configure database**

   For production with PostgreSQL:
   - Update `smart_inventory/settings.py` with your PostgreSQL credentials:
   
```
python
   DATABASES = {
       'default': {
           'ENGINE': 'django.db.backends.postgresql',
           'NAME': 'smart_inventory_db',
           'USER': 'postgres',
           'PASSWORD': 'your_password',# your_actual_password
           'HOST': 'localhost',
           'PORT': '5432',
       }
   }
   
```

5. **Run migrations**
   
```
bash
   python manage.py migrate
   
```

6. **Create a superuser**
   
```
bash
   python manage.py createsuperuser
   
```

7. **Run the development server**
   
```
bash
   python manage.py runserver
   
```

8. **Access the application**
   - Open your browser and navigate to: `http://127.0.0.1:8000/`
   - Admin panel: `http://127.0.0.1:8000/admin/`

## 📖 Usage

### Django Admin Panel
Access the admin panel at `/admin/` to manage:
- Users and permissions
- Products
- Suppliers
- Inventory
- Purchases
- Sales

### API Endpoints
If REST API is enabled, access endpoints at:
- `/api/` - API root
- `/api/products/` - Products API
- `/api/suppliers/` - Suppliers API
- `/api/purchases/` - Purchases API
- `/api/sales/` - Sales API

### Data Export
The application supports exporting data to Excel format using openpyxl.

## 🔧 Development Commands

```
bash
# Create migrations
python manage.py makemigrations

# Apply migrations
python manage.py migrate

# Create a new app
python manage.py startapp appname

# Collect static files
python manage.py collectstatic

# Check for issues
python manage.py check

# Shell access
python manage.py shell
```

## 📝 Project Apps

### 1. Products App (`products/`)
Manages product catalog and details.

### 2. Suppliers App (`suppliers/`)
Handles supplier information and relationships.

### 3. Inventory App (`inventory/`)
Tracks inventory levels and stock movements.

### 4. Purchases App (`purchases/`)
Records purchase orders and supplier transactions.

### 5. Sales App (`sales/`)
Manages sales transactions and revenue tracking.

### 6. Users App (`users/`)
Handles user authentication and authorization.

## 🔐 Security Notes

- Keep the `SECRET_KEY` in `settings.py` confidential
- Update `ALLOWED_HOSTS` for production deployments
- Use environment variables for sensitive configuration
- Enable HTTPS in production

## 📄 License

This project is part of Team Software Project course.

## 👥 Team

- STACK5
- [Team Members]
- W.R.U.Premarathna - 2022/CSC/077
- L.D Wanigasekara -2022/CSC/040
- W.M.N.B. Wijesooriya -2022/CSC/079
- S.A.C.T.S Subasinghe -2022/CSC/073
- Premkumar Kiritharan -2022/CSC/058

## 📞 Support

For issues or questions, please contact the development team.

---

*Last updated: 2026/02/16*
