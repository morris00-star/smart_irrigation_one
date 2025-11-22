import logging

import pytz
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.core.cache import cache
from django.utils import timezone
from datetime import timedelta, datetime
from .models import SensorData, ControlCommand, Threshold, SystemConfiguration, DeviceStatus, Schedule, UserPreference
from django.contrib.auth import get_user_model
from .sms import send_irrigation_alert
from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync

logger = logging.getLogger(__name__)
User = get_user_model()

# Constants
DEFAULT_THRESHOLD = 30
MANUAL_MODE_DURATION = timedelta(hours=1)

# Set to East Africa Time (EAT)
EAT = pytz.timezone('Africa/Nairobi')


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def receive_sensor_data(request):
    logger.info(f"[API] Incoming sensor data from {request.META.get('REMOTE_ADDR')}")
    logger.debug(f"[DATA] {request.data}")

    if request.method == 'POST':
        try:
            data = request.data
            user = request.user
            logger.info(f"[USER] Processing data for {user.username}")

            # Convert 'NA' strings to None
            def clean_value(val):
                if val in ('NA', None, '', 'null'):
                    return None
                try:
                    return int(val)
                except (ValueError, TypeError):
                    return None

            # Save sensor data
            sensor_data = SensorData.objects.create(
                moisture=clean_value(data.get('moisture')),
                pump_status=data.get('pump_status', False),
                threshold=data.get('threshold', DEFAULT_THRESHOLD),
                user=user
            )

            # Update cache
            cache_keys = {
                f'moisture_{user.id}': sensor_data.moisture,
                f'pump_state_{user.id}': 'on' if sensor_data.pump_status else 'off',
                f'threshold_{user.id}': sensor_data.threshold
            }

            for key, value in cache_keys.items():
                cache.set(key, value, timeout=None)

            # Send WebSocket update
            try:
                channel_layer = get_channel_layer()
                async_to_sync(channel_layer.group_send)(
                    f"sensor_updates_{user.id}",
                    {
                        "type": "send_sensor_data",
                        "data": {
                            "moisture": sensor_data.moisture,
                            "pump_status": sensor_data.pump_status,
                            "timestamp": sensor_data.timestamp.isoformat()
                        }
                    }
                )
                logger.debug("[WEBSOCKET] Update sent")
            except Exception as ws_error:
                logger.error(f"[WEBSOCKET] Error: {ws_error}")

            # Send alerts if needed
            send_irrigation_alert(user, sensor_data)

            logger.info("[API] Data processed successfully")
            return Response({"status": "success"}, status=status.HTTP_201_CREATED)

        except Exception as e:
            logger.error(f"[ERROR] Type: {type(e)}, Message: {str(e)}", exc_info=True)
            return Response({
                "status": "error",
                "message": str(e),
                "type": type(e).__name__
            }, status=status.HTTP_400_BAD_REQUEST)
    return None


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def control_system(request):
    logger.info(f"[CONTROL] Request from {request.META.get('REMOTE_ADDR')}")
    logger.debug(f"[CONTROL DATA] {request.data}")

    try:
        action = request.data.get('action')
        user = request.user
        user_id = user.id

        # Helper function to create control command
        def create_control_command(pump=None, manual=None, emergency=None):
            return ControlCommand.objects.create(
                pump_status=pump if pump is not None else False,
                manual_mode=manual if manual is not None else False,
                emergency=emergency if emergency is not None else False,
                user=user
            )

        if action == 'toggle_pump':
            # Only allow pump toggle in manual mode
            if not cache.get(f'system_mode_{user_id}', False):
                logger.warning("[PUMP] Attempted toggle while not in manual mode")
                return Response(
                    {"error": "System must be in manual mode to control pump"},
                    status=status.HTTP_403_FORBIDDEN
                )

            # Verify the requested state is different from current state
            current_pump_state = cache.get(f'pump_state_{user_id}', 'off')
            requested_state = request.data.get('state', False)
            requested_state_str = 'on' if requested_state else 'off'

            if current_pump_state == requested_state_str:
                logger.warning(f"[PUMP] Already in requested state: {requested_state_str}")
                return Response({"pump": current_pump_state})

            # Only proceed if state is actually changing
            cache.set(f'pump_state_{user_id}', requested_state_str, timeout=None)
            create_control_command(pump=requested_state)
            logger.info(f"[PUMP] State changed to {requested_state_str}")
            return Response({"pump": requested_state_str})

        elif action == 'set_threshold':
            threshold = request.data.get('threshold')
            if threshold is None:
                logger.warning("[THRESHOLD] No value provided")
                return Response({"error": "Threshold value required"}, status=400)

            try:
                threshold = int(threshold)
                if not (0 <= threshold <= 100):
                    raise ValueError("Threshold must be between 0 and 100")
            except ValueError as e:
                logger.error(f"[THRESHOLD] Invalid value: {threshold}")
                return Response({"error": str(e)}, status=400)

            Threshold.objects.create(threshold=threshold, user=user)
            cache.set(f'threshold_{user_id}', threshold, timeout=None)
            logger.info(f"[THRESHOLD] Set to {threshold}%")
            return Response({"threshold": threshold})

        elif action == 'set_mode':
            manual_mode = request.data.get('manual_mode', False)
            cache.set(f'system_mode_{user_id}', manual_mode, timeout=None)
            # Get current states from cache
            pump_state = cache.get(f'pump_state_{user_id}', 'off') == 'on'
            emergency_state = cache.get(f'emergency_{user_id}', False)
            # When switching to auto mode, ensure pump is off
            if not manual_mode:
                pump_state = False
                cache.set(f'pump_state_{user_id}', 'off', timeout=None)
                logger.info("[MODE] Switched to auto mode - ensuring pump is off")
            # Create control command with all required fields
            ControlCommand.objects.create(
                pump_status=pump_state,
                manual_mode=manual_mode,
                emergency=emergency_state,
                user=user
            )
            logger.info(f"[MODE] Changed to {'manual' if manual_mode else 'auto'}")
            return Response({
                "manual_mode": manual_mode,
                "pump": "off" if not manual_mode else ("on" if pump_state else "off")
            })

        elif action == 'emergency_stop':
            cache.set(f'emergency_{user_id}', True, timeout=None)
            config = SystemConfiguration.get_for_user(user)
            config.emergency_stop = True
            config.save()
            cache.set(f'pump_state_{user_id}', 'off', timeout=None)

            # Create control command with all required fields
            ControlCommand.objects.create(
                emergency=True,
                pump_status=False,
                manual_mode=False,
                user=user
            )

            logger.warning("[EMERGENCY] Stop activated")
            return Response({
                "emergency": True,
                "pump": "off"
            })

        elif action == 'reset_emergency':
            current_emergency = cache.get(f'emergency_{user_id}', False)
            cache.set(f'emergency_{user_id}', False, timeout=None)
            config = SystemConfiguration.get_for_user(user)
            config.emergency_stop = False
            config.save()
            if not current_emergency:
                logger.warning("[EMERGENCY] No active emergency to reset")
                return Response({
                    "status": "no_active_emergency",
                    "emergency": False,
                    "system_active": cache.get(f'system_active_{user_id}', False),
                    "pump": cache.get(f'pump_state_{user_id}', 'off')
                })
            cache.set(f'emergency_{user_id}', False, timeout=None)
            # Convert string states to boolean for database
            pump_state = cache.get(f'pump_state_{user_id}', 'off') == 'on'
            manual_mode = cache.get(f'system_mode_{user_id}', False)
            ControlCommand.objects.create(
                emergency=False,
                pump_status=pump_state,
                manual_mode=manual_mode,
                user=user
            )

            logger.info("[EMERGENCY] Reset")
            return Response({
                "status": "emergency_reset",
                "emergency": False,
                "system_active": cache.get(f'system_active_{user_id}', False),
                "pump": cache.get(f'pump_state_{user_id}', 'off')
            })

        elif action == 'disconnect':
            # Clear connection status
            cache.set(f'device_connection_{user_id}', False, timeout=None)
            logger.info("[CONNECTION] Manually disconnected by user")
            return Response({
                "status": "disconnected",
                "connected": False
            })

        elif action == 'get_state':
            # Get current state from cache with safe defaults
            response_data = {
                "pump": cache.get(f'pump_state_{user_id}', 'off'),
                "manual_mode": cache.get(f'system_mode_{user_id}', False),
                "emergency": cache.get(f'emergency_{user_id}', False),
                "threshold": cache.get(f'threshold_{user_id}', DEFAULT_THRESHOLD),
                "irrigation_active": False,
                "connected": cache.get(f'device_connection_{user_id}', False),
                "last_seen": cache.get(f'device_last_seen_{user_id}'),
            }

            # Only allow irrigation when in manual mode and not in emergency
            if response_data['manual_mode'] and not response_data['emergency']:
                response_data['irrigation_active'] = response_data['pump'] == 'on'

            # Get next scheduled irrigation if exists
            next_schedule = Schedule.objects.filter(
                user=user,
                is_active=True,
                scheduled_time__gte=timezone.now()
            ).order_by('scheduled_time').first()

            if next_schedule:
                response_data["schedule"] = {
                    "year": next_schedule.scheduled_time.year,
                    "date": next_schedule.scheduled_time.date().isoformat(),
                    "time": next_schedule.scheduled_time.time().isoformat(),
                    "duration": next_schedule.duration
                }

            # Get latest sensor data
            latest_data = SensorData.objects.filter(user=user).order_by('-timestamp').first()
            if latest_data:
                response_data.update({
                    "timestamp": latest_data.timestamp.isoformat()
                })
            else:
                response_data.update({
                    "timestamp": timezone.now().isoformat()
                })

            logger.debug("[STATE] Current state sent")
            return Response(response_data)

        logger.warning(f"[CONTROL] Invalid action: {action}")
        return Response({"error": "Invalid action"}, status=400)

    except Exception as e:
        logger.error(f"[CONTROL ERROR] {str(e)}", exc_info=True)
        return Response({"error": str(e)}, status=500)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def get_system_status(request):
    """
    Fetch current system status including sensor readings.
    """
    try:
        user = request.user
        user_id = user.id
        latest_data = SensorData.objects.filter(user=user).order_by('-timestamp').first()

        def format_value(value):
            return value if value is not None else 'NA'

        return Response({
            "pump": cache.get(f'pump_state_{user_id}', 'off'),
            "moisture": format_value(latest_data.moisture if latest_data else None),
            "threshold": cache.get(f'threshold_{user_id}', DEFAULT_THRESHOLD),
            "system_mode": cache.get(f'system_mode_{user_id}', False),
            "emergency": cache.get(f'emergency_{user_id}', False),
            "timestamp": latest_data.timestamp.isoformat() if latest_data else timezone.now().isoformat()
        })
    except Exception as e:
        logger.error(f"Status error: {e}")
        return Response({"error": str(e)}, status=500)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def save_configuration(request):
    """Save system configuration (crop type, soil type, threshold)."""
    try:
        user = request.user
        crop = request.data.get('crop')
        soil = request.data.get('soil')
        threshold = request.data.get('threshold')

        # Get or create user preferences
        preferences, created = UserPreference.objects.get_or_create(user=user)

        # Update fields if provided
        if crop is not None:
            preferences.crop_type = crop
        if soil is not None:
            preferences.soil_type = soil
        if threshold is not None:
            preferences.soil_moisture_threshold = threshold

        preferences.save()

        # Update cache
        cache.set(f'crop_{user.id}', preferences.crop_type, timeout=None)
        cache.set(f'soil_{user.id}', preferences.soil_type, timeout=None)
        cache.set(f'threshold_{user.id}', preferences.soil_moisture_threshold, timeout=None)

        return Response({
            "status": "success",
            "crop": preferences.crop_type,
            "soil": preferences.soil_type,
            "threshold": preferences.soil_moisture_threshold,
            "recommended_threshold": preferences.recommended_threshold,
            "threshold_suggestion": preferences.get_threshold_suggestion()
        })

    except Exception as e:
        logger.error(f"Error saving configuration: {e}")
        return Response({"error": str(e)}, status=500)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def get_configuration(request):
    """Get current system configuration."""
    try:
        user = request.user
        preferences = UserPreference.objects.filter(user=user).first()

        if not preferences:
            return Response({
                "crop": None,
                "soil": None,
                "threshold": DEFAULT_THRESHOLD,
                "recommended_threshold": DEFAULT_THRESHOLD,
                "threshold_suggestion": "Please configure your crop and soil type"
            })

        return Response({
            "crop": preferences.crop_type,
            "soil": preferences.soil_type,
            "threshold": preferences.soil_moisture_threshold,
            "recommended_threshold": preferences.recommended_threshold,
            "threshold_suggestion": preferences.get_threshold_suggestion()
        })
    except Exception as e:
        logger.error(f"Error getting configuration: {e}")
        return Response({"error": str(e)}, status=500)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def watering_history(request):
    """
    Get watering history for the user.
    """
    try:
        user = request.user
        # Get last 20 watering events (pump activations)
        history = SensorData.objects.filter(
            user=user,
            pump_status=True
        ).order_by('-timestamp')[:20]

        return Response([{
            "timestamp": data.timestamp.isoformat(),
            "duration": 5
        } for data in history])

    except Exception as e:
        logger.error(f"Error getting watering history: {e}")
        return Response({"error": str(e)}, status=500)


# Endpoint to handle notes
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def add_note(request):
    try:
        user = request.user
        note_text = request.data.get('note')

        if not note_text:
            return Response({"error": "Note text is required"}, status=status.HTTP_400_BAD_REQUEST)

        # In a real implementation, you would save to a Note model
        cache_key = f'notes_{user.id}'
        notes = cache.get(cache_key, [])
        notes.append({
            'text': note_text,
            'timestamp': timezone.now().isoformat()
        })
        cache.set(cache_key, notes, timeout=None)

        return Response({"status": "success"})

    except Exception as e:
        logger.error(f"Error adding note: {e}")
        return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['GET', 'POST', 'PUT', 'DELETE'])
@permission_classes([IsAuthenticated])
def schedule_irrigation(request, schedule_id=None):
    user = request.user

    # Check system mode and emergency status
    if request.method in ['POST', 'PUT', 'DELETE']:
        system_mode = cache.get(f'system_mode_{user.id}', False)
        emergency = cache.get(f'emergency_{user.id}', False)

        if not system_mode or emergency:
            return Response(
                {'error': 'Scheduling is only available in manual mode when no emergency is active'},
                status=status.HTTP_403_FORBIDDEN
            )

    # GET - List all schedules (always allowed)
    if request.method == 'GET':
        schedules = Schedule.objects.filter(user=user).order_by('scheduled_time')
        return Response([{
            'id': s.id,
            'scheduled_time': s.scheduled_time.isoformat(),
            'duration': s.duration,
            'is_active': s.is_active
        } for s in schedules])

    # POST - Create new schedule
    elif request.method == 'POST':
        try:
            data = request.data
            scheduled_time_str = data.get('scheduled_time')
            duration = int(data.get('duration', 15))

            if not scheduled_time_str:
                return Response({'error': 'Scheduled time is required'}, status=400)

            try:
                scheduled_time = datetime.fromisoformat(scheduled_time_str.replace('Z', '+00:00'))
            except ValueError:
                return Response({'error': 'Invalid datetime format'}, status=400)

            if scheduled_time < timezone.now():
                return Response({'error': 'Scheduled time must be in the future'}, status=400)

            schedule = Schedule.objects.create(
                user=user,
                scheduled_time=scheduled_time,
                duration=duration
            )

            return Response({
                'id': schedule.id,
                'scheduled_time': schedule.scheduled_time.isoformat(),
                'duration': schedule.duration
            }, status=201)

        except Exception as e:
            return Response({'error': str(e)}, status=400)

    # PUT - Update existing schedule
    elif request.method == 'PUT' and schedule_id:
        try:
            schedule = Schedule.objects.get(id=schedule_id, user=user)
            data = request.data

            if 'scheduled_time' in data:
                try:
                    scheduled_time = datetime.fromisoformat(data['scheduled_time'].replace('Z', '+00:00'))
                    if scheduled_time < timezone.now():
                        return Response({'error': 'Scheduled time must be in the future'}, status=400)
                    schedule.scheduled_time = scheduled_time
                except ValueError:
                    return Response({'error': 'Invalid datetime format'}, status=400)

            if 'duration' in data:
                duration = int(data['duration'])
                if duration < 1 or duration > 120:
                    return Response({'error': 'Duration must be between 1-120 minutes'}, status=400)
                schedule.duration = duration

            schedule.save()
            return Response({
                'id': schedule.id,
                'scheduled_time': schedule.scheduled_time.isoformat(),
                'duration': schedule.duration
            })

        except Schedule.DoesNotExist:
            return Response({'error': 'Schedule not found'}, status=404)
        except Exception as e:
            return Response({'error': str(e)}, status=400)

    # DELETE - Remove schedule
    elif request.method == 'DELETE' and schedule_id:
        try:
            schedule = Schedule.objects.get(id=schedule_id, user=user)
            schedule.delete()
            return Response({'status': 'success'})
        except Schedule.DoesNotExist:
            return Response({'error': 'Schedule not found'}, status=404)
        except Exception as e:
            return Response({'error': str(e)}, status=400)

    return Response({'error': 'Invalid request'}, status=400)


@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated])
def schedule_list(request):
    """Handle listing and creation of schedules"""
    if request.method == 'GET':
        schedules = Schedule.objects.filter(user=request.user).order_by('scheduled_time')
        return Response([{
            'id': s.id,
            'scheduled_time': s.scheduled_time.astimezone(EAT).isoformat(),
            'duration': s.duration,
            'is_active': s.is_active
        } for s in schedules])

    elif request.method == 'POST':
        try:
            data = request.data
            scheduled_time_str = data.get('scheduled_time', None)
            duration = data.get('duration', None)

            # Validate required fields
            if not scheduled_time_str or not duration:
                return Response({'error': 'Scheduled time and duration are required'}, status=400)

            try:
                # Parse and convert to EAT timezone
                scheduled_time = datetime.fromisoformat(scheduled_time_str.replace('Z', '+00:00'))
                scheduled_time = scheduled_time.astimezone(EAT)
            except ValueError:
                return Response({'error': 'Invalid datetime format'}, status=400)

            if scheduled_time < timezone.now().astimezone(EAT):
                return Response({'error': 'Scheduled time must be in the future'}, status=400)

            schedule = Schedule.objects.create(
                user=request.user,
                scheduled_time=scheduled_time,
                duration=duration
            )

            return Response({
                'id': schedule.id,
                'scheduled_time': schedule.scheduled_time.astimezone(EAT).isoformat(),
                'duration': schedule.duration
            }, status=201)

        except Exception as e:
            return Response({'error': str(e)}, status=400)
    return None


@api_view(['GET', 'PUT', 'DELETE'])
@permission_classes([IsAuthenticated])
def schedule_detail(request, pk):
    """Handle retrieval, update and deletion of individual schedules"""
    schedule = get_object_or_404(Schedule, pk=pk, user=request.user)

    if request.method == 'GET':
        return Response({
            'id': schedule.id,
            'scheduled_time': schedule.scheduled_time.astimezone(EAT).isoformat(),
            'duration': schedule.duration,
            'is_active': schedule.is_active
        })

    elif request.method == 'PUT':
        try:
            data = request.data

            if 'scheduled_time' in data:
                try:
                    scheduled_time = datetime.fromisoformat(data['scheduled_time'].replace('Z', '+00:00'))
                    scheduled_time = scheduled_time.astimezone(EAT)
                    if scheduled_time < timezone.now().astimezone(EAT):
                        return Response({'error': 'Scheduled time must be in the future'}, status=400)
                    schedule.scheduled_time = scheduled_time
                except ValueError:
                    return Response({'error': 'Invalid datetime format'}, status=400)

            if 'duration' in data:
                duration = data['duration']
                if not duration:
                    return Response({'error': 'Duration cannot be empty'}, status=400)
                schedule.duration = duration

            schedule.save()
            return Response({
                'id': schedule.id,
                'scheduled_time': schedule.scheduled_time.astimezone(EAT).isoformat(),
                'duration': schedule.duration
            })

        except Exception as e:
            return Response({'error': str(e)}, status=400)

    elif request.method == 'DELETE':
        try:
            schedule.delete()
            return Response({'status': 'success'})
        except Exception as e:
            return Response({'error': str(e)}, status=400)
    return None


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def device_heartbeat(request):
    """
    Endpoint for devices to send periodic status updates
    """
    try:
        user = request.user
        data = request.data
        device_id = data.get('device_id', 'default_device')

        # Create or update device status
        status_data = {
            'system_mode': data.get('system_mode', 'auto'),
            'ip_address': request.META.get('REMOTE_ADDR'),
            'firmware': data.get('firmware', 'unknown')
        }

        device_status, created = DeviceStatus.objects.update_or_create(
            user=user,
            device_id=device_id,
            defaults={
                'operational_mode': status_data['system_mode'],
                'status_data': status_data,
                'ip_address': status_data['ip_address'],
                'firmware_version': status_data['firmware']
            }
        )

        # Update cache with latest status
        cache_keys = {
            f'device_{device_id}_status': status_data,
            f'device_{device_id}_last_seen': timezone.now().isoformat()
        }
        for key, value in cache_keys.items():
            cache.set(key, value, timeout=3600)  # 1 hour cache

        return Response({"status": "success", "device_id": device_id})

    except Exception as e:
        logger.error(f"[DEVICE HEARTBEAT] Error: {str(e)}", exc_info=True)
        return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
