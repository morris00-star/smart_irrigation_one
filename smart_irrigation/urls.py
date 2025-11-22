from django.urls import path, include
from django.contrib import admin
from accounts.views import home
from irrigation import views as irrigation_views
from irrigation import api
from django.conf import settings
from django.conf.urls.static import static


urlpatterns = [
    path('admin/', admin.site.urls),
    path('', home, name='home'),
    path('irrigation/', include('irrigation.urls')),  # Irrigation app URLs
    path('accounts/', include('accounts.urls')),
    path('about/', irrigation_views.about, name='about'),
    path('contact/', irrigation_views.contact, name='contact'),
    path('help/', irrigation_views.help, name='help'),
    path('__debug__/', include('debug_toolbar.urls')),
    path('keep-alive/', irrigation_views.keep_alive, name='keep-alive'),

    # API URLs
    path('api/sensor_data/', api.receive_sensor_data, name='receive_sensor_data'),
    path('api/control/', api.control_system, name='control_system'),
    path('api/status/', api.get_system_status, name='get_system_status'),
    path('api/save_config/', api.save_configuration, name='save_configuration'),
    path('api/get_config/', api.get_configuration, name='get_configuration'),
    path('api/watering_history/', api.watering_history, name='watering_history'),
    path('api/add-note/', api.add_note, name='add-note'),
    path('api/schedule/', api.schedule_irrigation, name='schedule-irrigation'),
    path('api/device-heartbeat/', api.device_heartbeat, name='device-heartbeat'),
    path('api/schedule/', api.schedule_list, name='schedule-list'),
    path('api/schedule/<int:pk>/', api.schedule_detail, name='schedule-detail'),

] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)


# Only serve media files in development, not in production
if settings.DEBUG and not settings.IS_PRODUCTION:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
