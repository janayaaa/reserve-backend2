from django.urls import path
from .views import (
    ActivityLogView,
    AvailableDonationsView,
    DashboardView,
    DonationQRVerifyView,
    DonationRequestView,
    DonationRespondView,
    FoodBankAnnouncementsView,
    FoodBankProfilePortalView,
    FoodTypesView,
    PartnersView,
)

urlpatterns = [
    path('profile/<str:org_id>/', FoodBankProfilePortalView.as_view(), name='foodbank-profile-portal'),
    path('requests/', DonationRequestView.as_view(), name='donation-requests'),
    path('food-types/', FoodTypesView.as_view(), name='food-types'),
    path('available/', AvailableDonationsView.as_view(), name='available-donations'),
    path('respond/<int:donation_id>/', DonationRespondView.as_view(), name='respond-donation'),
    path('verify-qr/', DonationQRVerifyView.as_view(), name='verify-donation-qr'),
    path('activity/', ActivityLogView.as_view(), name='activity-log'),
    path('announcements/', FoodBankAnnouncementsView.as_view(), name='foodbank-announcements'),
    path('dashboard/', DashboardView.as_view(), name='foodbank-dashboard'),
    path('partners/', PartnersView.as_view(), name='foodbank-partners'),
]