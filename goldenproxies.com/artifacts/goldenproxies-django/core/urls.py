from django.urls import path
from . import views

urlpatterns = [
    path('', views.home, name='home'),
    path('pricing/', views.pricing, name='pricing'),
    path('contact/', views.contact, name='contact'),

    # Blog (public)
    path('blog/', views.blog_list, name='blog_list'),
    path('blog/<slug:slug>/', views.blog_detail, name='blog_detail'),

    path('login/', views.login_view, name='login'),
    path('register/', views.register_view, name='register'),
    path('logout/', views.logout_view, name='logout'),

    path('dashboard/', views.dashboard_overview, name='dashboard'),
    path('dashboard/generator/', views.dashboard_generator, name='dashboard_generator'),
    path('dashboard/stats/', views.dashboard_stats, name='dashboard_stats'),
    path('dashboard/support/', views.dashboard_support, name='dashboard_support'),
    path('dashboard/support/<int:ticket_id>/', views.dashboard_support_detail, name='dashboard_support_detail'),
    path('dashboard/support/<int:ticket_id>/edit/', views.dashboard_support_edit, name='dashboard_support_edit'),
    path('dashboard/support/<int:ticket_id>/delete/', views.dashboard_support_delete, name='dashboard_support_delete'),
    path('dashboard/settings/', views.dashboard_settings, name='dashboard_settings'),
    path('dashboard/change-password/', views.change_password, name='change_password'),
    path('dashboard/generate-api-key/', views.generate_api_key, name='generate_api_key'),
    path('dashboard/close-account/', views.close_account, name='close_account'),

    # Billing
    path('billing/', views.billing_dashboard, name='billing_dashboard'),
    path('billing/checkout/<str:plan>/<str:period>/', views.billing_checkout, name='billing_checkout'),
    path('billing/checkout/<str:plan>/', views.billing_checkout, name='billing_checkout_default'),
    path('billing/success/', views.billing_success, name='billing_success'),
    path('billing/cancel/', views.billing_cancel, name='billing_cancel'),
    path('billing/portal/', views.billing_portal, name='billing_portal'),
    path('billing/webhook/', views.billing_webhook, name='billing_webhook'),

    # Admin
    path('admin-panel/', views.admin_overview, name='admin_overview'),
    path('admin-panel/users/', views.admin_users, name='admin_users'),
    path('admin-panel/users/<int:user_id>/', views.admin_user_detail, name='admin_user_detail'),
    path('admin-panel/purchases/', views.admin_purchases, name='admin_purchases'),
    path('admin-panel/messages/', views.admin_messages, name='admin_messages'),
    path('admin-panel/messages/<int:msg_id>/reply/', views.admin_reply, name='admin_reply'),
    path('admin-panel/messages/<int:msg_id>/status/', views.admin_message_status, name='admin_message_status'),
    path('admin-panel/invoices/', views.admin_invoices, name='admin_invoices'),
    path('admin-panel/whop-settings/', views.admin_whop_settings, name='admin_whop_settings'),
    path('admin-panel/whop-resync-all/', views.admin_whop_resync_all, name='admin_whop_resync_all'),
    path('admin-panel/users/<int:user_id>/ban/', views.admin_toggle_ban, name='admin_toggle_ban'),
    path('admin-panel/users/<int:user_id>/delete/', views.admin_delete_user, name='admin_delete_user'),

    # Admin blog
    path('admin-panel/blog/', views.admin_blog_list, name='admin_blog_list'),
    path('admin-panel/blog/generate/', views.admin_blog_generate, name='admin_blog_generate'),
    path('admin-panel/blog/scrape/', views.admin_blog_scrape, name='admin_blog_scrape'),
    path('admin-panel/blog/rewrite/', views.admin_blog_rewrite, name='admin_blog_rewrite'),
    path('admin-panel/blog/publish/', views.admin_blog_publish, name='admin_blog_publish'),
    path('admin-panel/blog/new/', views.admin_blog_edit, name='admin_blog_new'),
    path('admin-panel/blog/<int:post_id>/edit/', views.admin_blog_edit, name='admin_blog_edit'),
    path('admin-panel/blog/<int:post_id>/publish/', views.admin_blog_toggle_status, name='admin_blog_toggle_status'),
    path('admin-panel/blog/<int:post_id>/delete/', views.admin_blog_delete, name='admin_blog_delete'),

    # Admin API settings
    path('admin-panel/api-settings/', views.admin_api_settings, name='admin_api_settings'),

    # Admin Email settings
    path('admin-panel/email-settings/', views.admin_email_settings, name='admin_email_settings'),
    path('admin-panel/email-settings/send-test/', views.admin_send_test_email, name='admin_send_test_email'),
]
