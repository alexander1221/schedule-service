from django.urls import path

from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("login/", views.login_email, name="login"),
    path("login/code/", views.login_code, name="login_code"),
    path("logout/", views.logout_view, name="logout"),
    path("me/", views.me, name="me"),
    path("me/calendar.ics", views.my_calendar, name="my_calendar"),
    path("me/calendar/link/", views.calendar_link, name="calendar_link"),
    path("calendar/<str:token>.ics", views.calendar_by_token, name="calendar_by_token"),
]
