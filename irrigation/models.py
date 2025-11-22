from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.contrib.auth import get_user_model
from django.utils import timezone

User = get_user_model()

# Define the default threshold at the module level
DEFAULT_THRESHOLD = 50


class SensorData(models.Model):
    moisture = models.IntegerField(null=True, blank=True)
    pump_status = models.BooleanField(default=False)
    threshold = models.IntegerField(default=DEFAULT_THRESHOLD)
    timestamp = models.DateTimeField(auto_now_add=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, null=True, blank=True)

    def __str__(self):
        return f"Moisture: {self.moisture}% at {self.timestamp}"


class ControlCommand(models.Model):
    pump_status = models.BooleanField(default=False)
    manual_mode = models.BooleanField(default=False)
    emergency = models.BooleanField(default=False)
    threshold = models.IntegerField(null=True, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, null=True, blank=True)

    def __str__(self):
        actions = []
        if self.pump_status:
            actions.append("Pump ON")
        if self.manual_mode:
            actions.append("Manual Mode")
        if self.emergency:
            actions.append("EMERGENCY")
        if self.threshold:
            actions.append(f"Threshold: {self.threshold}%")

        return f"Control at {self.timestamp}: {', '.join(actions) or 'No action'}"


class Threshold(models.Model):
    threshold = models.IntegerField(default=DEFAULT_THRESHOLD)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, null=True, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Historical Threshold"
        verbose_name_plural = "Historical Thresholds"

    def __str__(self):
        return f"Threshold: {self.threshold}% (User: {self.user}, Time: {self.timestamp})"


class SystemConfiguration(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    emergency_stop = models.BooleanField(default=False)
    last_updated = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "System Configuration"
        verbose_name_plural = "System Configurations"

    def __str__(self):
        return f"{self.user.username}'s System Config"

    @classmethod
    def get_for_user(cls, user):
        config, created = cls.objects.get_or_create(user=user)
        return config


class UserPreference(models.Model):
    CROP_CHOICES = [
        ('banana', 'Banana'),
        ('maize', 'Maize'),
        ('beans', 'Beans'),
        ('coffee', 'Coffee'),
        ('cassava', 'Cassava'),
        ('rice', 'Rice'),
        ('tomato', 'Tomato'),
        ('potato', 'Potato'),
        ('sugarcane', 'Sugarcane'),
        ('vegetables', 'Vegetables'),
    ]

    SOIL_CHOICES = [
        ('clay', 'Clay'),
        ('loamy', 'Loamy'),
        ('sandy', 'Sandy'),
    ]

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='preferences')
    crop_type = models.CharField(max_length=50, choices=CROP_CHOICES, blank=True, null=True)
    soil_type = models.CharField(max_length=50, choices=SOIL_CHOICES, blank=True, null=True)
    soil_moisture_threshold = models.IntegerField(default=DEFAULT_THRESHOLD, help_text="Optimal soil moisture threshold for the selected crop/soil (%)")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "User Preference"
        verbose_name_plural = "User Preferences"

    def __str__(self):
        crop_display = self.get_crop_type_display() or "Not Set"
        soil_display = self.get_soil_type_display() or "Not Set"
        return f"{self.user.username}'s Preferences: {crop_display} ({soil_display}), Threshold: {self.soil_moisture_threshold}%"

    def get_optimal_threshold(self):
        return self.soil_moisture_threshold

    @property
    def recommended_threshold(self):
        if not self.crop_type or not self.soil_type:
            return DEFAULT_THRESHOLD

        recommendations = {
            'banana': {
                'clay': 45,
                'loamy': 40,
                'sandy': 35,
            },
            'maize': {
                'clay': 50,
                'loamy': 45,
                'sandy': 40,
            },
            'beans': {
                'clay': 55,
                'loamy': 50,
                'sandy': 45,
            },
            'coffee': {
                'clay': 60,
                'loamy': 55,
                'sandy': 50,
            },
            'cassava': {
                'clay': 40,
                'loamy': 35,
                'sandy': 30,
            },
            'rice': {
                'clay': 70,
                'loamy': 65,
                'sandy': 60,
            },
            'tomato': {
                'clay': 50,
                'loamy': 45,
                'sandy': 40,
            },
            'potato': {
                'clay': 45,
                'loamy': 40,
                'sandy': 35,
            },
            'sugarcane': {
                'clay': 55,
                'loamy': 50,
                'sandy': 45,
            },
            'vegetables': {
                'clay': 50,
                'loamy': 45,
                'sandy': 40,
            },
        }

        return recommendations.get(self.crop_type, {}).get(self.soil_type, DEFAULT_THRESHOLD)

    def get_threshold_suggestion(self):
        if not self.crop_type or not self.soil_type:
            return "Using default threshold. Please select crop and soil type for personalized recommendations."

        explanations = {
            'clay': "Clay soil retains more water, so we recommend a higher threshold to prevent overwatering.",
            'loamy': "Loamy soil has balanced water retention, so we recommend a moderate threshold.",
            'sandy': "Sandy soil drains quickly, so we recommend a lower threshold to ensure adequate watering.",
        }

        crop_name = self.get_crop_type_display()
        soil_name = self.get_soil_type_display()
        soil_explanation = explanations.get(self.soil_type, "")

        return (
            f"For {crop_name} in {soil_name} soil, we recommend a threshold of {self.recommended_threshold}%. "
            f"{soil_explanation}"
        )


class DeviceStatus(models.Model):
    OPERATIONAL_MODES = [
        ('auto', 'Automatic'),
        ('manual', 'Manual'),
    ]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    device_id = models.CharField(max_length=50)
    emergency_status = models.BooleanField(default=False)
    operational_mode = models.CharField(max_length=10, choices=OPERATIONAL_MODES, default='auto')
    pump_status = models.BooleanField(default=False)
    moisture_threshold = models.IntegerField(default=DEFAULT_THRESHOLD)
    last_contact = models.DateTimeField(auto_now=True)
    status_data = models.JSONField(default=dict)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    firmware_version = models.CharField(max_length=20, blank=True, null=True)

    class Meta:
        verbose_name_plural = "Device Statuses"
        ordering = ['-last_contact']

    def __str__(self):
        return f"{self.device_id} - {self.user.username} ({self.last_contact})"


class Schedule(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    scheduled_time = models.DateTimeField()
    duration = models.PositiveIntegerField(help_text="Duration in minutes")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['scheduled_time']

    def __str__(self):
        return f"Scheduled irrigation for {self.user.username} at {self.scheduled_time} for {self.duration} minutes"

    def save(self, *args, **kwargs):
        if self.scheduled_time < timezone.now():
            raise ValidationError("Scheduled time must be in the future")
        super().save(*args, **kwargs)
