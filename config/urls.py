from django.contrib import admin
from django.conf import settings
from django.conf.urls.static import static
from django.urls import include, path

from .admin_navigation import configure_admin_navigation

admin.site.site_header = '탐방 통합 플랫폼 관리자'
admin.site.site_title = '탐방 통합 플랫폼 관리'
admin.site.index_title = '운영 관리'
configure_admin_navigation(admin.site)

urlpatterns = [
    path('admin/', admin.site.urls),
    path('surveys/', include('surveys.urls')),
    path('ops/', include('admins.urls')),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
