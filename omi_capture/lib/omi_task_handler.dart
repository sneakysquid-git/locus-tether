import 'dart:async';
import 'dart:io';

import 'package:flutter_blue_plus/flutter_blue_plus.dart';
import 'package:flutter_foreground_task/flutter_foreground_task.dart';
import 'package:opus_dart/opus_dart.dart';
import 'package:opus_flutter/opus_flutter.dart' as opus_flutter;

import 'omi_constants.dart';
import 'segmenting_audio_writer.dart';

const String outputDir = '/storage/emulated/0/Recordings/Omi';

/// Runs the actual BLE connection and continuous capture inside Android's
/// foreground service context, rather than the UI's main isolate - this is
/// the plugin's own documented pattern for a stream-based background data
/// source (their example does the same with a location stream).
@pragma('vm:entry-point')
void startCallback() {
  FlutterForegroundTask.setTaskHandler(OmiTaskHandler());
}

class OmiTaskHandler extends TaskHandler {
  BluetoothDevice? _device;
  BluetoothCharacteristic? _audioChar;
  BluetoothCharacteristic? _batteryChar;
  BluetoothCharacteristic? _settingsDimRatioChar;
  StreamSubscription<List<int>>? _audioSub;
  StreamSubscription<List<int>>? _batterySub;
  StreamSubscription<BluetoothConnectionState>? _connectionSub;
  SegmentingAudioWriter? _writer;

  Timer? _scanRetryTimer;
  Timer? _setupRetryTimer;
  bool _scanning = false;
  bool _settingUpAudio = false;
  bool _handlingDisconnect = false;
  bool _destroying = false;

  void _report(String status) {
    FlutterForegroundTask.updateService(notificationText: status);
    FlutterForegroundTask.sendDataToMain({'status': status});
  }

  @override
  Future<void> onStart(DateTime timestamp, TaskStarter starter) async {
    // Isolates don't share initialization state - the main isolate's
    // initOpus() call in main() never reaches this separate execution
    // context, which is exactly why every frame was failing to decode.
    initOpus(await opus_flutter.load());

    await _scanAndConnect();
  }

  Future<void> _scanAndConnect() async {
    if (_destroying || _device != null || _scanning) return;

    _scanning = true;
    _report('Scanning...');

    try {
      await FlutterBluePlus.adapterState
          .where((s) => s == BluetoothAdapterState.on)
          .first;

      final device = await _findOmiDevice();
      if (device == null) {
        _report('No Omi device found - retrying automatically...');
        _scheduleScanRetry();
        return;
      }

      await _enableAutoReconnect(device);
    } catch (e) {
      if (!_destroying) {
        _report('Scan failed: $e - retrying automatically...');
        _scheduleScanRetry();
      }
    } finally {
      _scanning = false;
    }
  }

  Future<BluetoothDevice?> _findOmiDevice() async {
    final completer = Completer<BluetoothDevice?>();

    late final StreamSubscription<List<ScanResult>> resultsSub;
    resultsSub = FlutterBluePlus.scanResults.listen((results) {
      if (completer.isCompleted) return;

      for (final r in results) {
        final isOmi = r.advertisementData.serviceUuids
                .any((u) => u.str.toLowerCase() == omiServiceUuid) ||
            r.advertisementData.advName.toLowerCase() == 'omi' ||
            r.device.platformName.toLowerCase() == 'omi';

        if (isOmi) {
          completer.complete(r.device);
          return;
        }
      }
    });

    try {
      await FlutterBluePlus.startScan(timeout: const Duration(seconds: 15));
      return await completer.future.timeout(
        const Duration(seconds: 15),
        onTimeout: () => null,
      );
    } finally {
      try {
        await FlutterBluePlus.stopScan();
      } catch (_) {
        // The timeout may already have stopped the scan.
      }
      await resultsSub.cancel();
    }
  }

  void _scheduleScanRetry() {
    _scanRetryTimer?.cancel();
    _scanRetryTimer = Timer(const Duration(seconds: 10), () {
      if (!_destroying && _device == null) {
        unawaited(_scanAndConnect());
      }
    });
  }

  Future<void> _enableAutoReconnect(BluetoothDevice device) async {
    _device = device;
    _scanRetryTimer?.cancel();

    await _connectionSub?.cancel();
    _connectionSub = device.connectionState.listen((state) {
      if (_destroying) return;

      if (state == BluetoothConnectionState.connected) {
        unawaited(_configureConnectedDevice(device));
      } else if (state == BluetoothConnectionState.disconnected) {
        unawaited(_handleDisconnected());
      }
    });

    _report('Connecting...');

    try {
      // autoConnect is deliberately used here instead of a one-shot BLE
      // connection. On Android it remains active across ordinary link drops
      // and Bluetooth toggles. mtu must be null when autoConnect is enabled;
      // we request the Omi's preferred MTU after each successful connection.
      await device.connect(
        license: License.nonprofit,
        autoConnect: true,
        mtu: null,
      );

      // connectionState normally emits the current state immediately, but
      // this covers the case where the device was already connected before
      // autoConnect was enabled.
      if (device.isConnected) {
        unawaited(_configureConnectedDevice(device));
      } else {
        _report('Waiting for Omi - auto-reconnect enabled...');
      }
    } catch (e) {
      if (!_destroying) {
        _report('Failed to enable auto-reconnect: $e');
      }
    }
  }

  Future<void> _configureConnectedDevice(BluetoothDevice device) async {
    if (_destroying || _settingUpAudio || !device.isConnected) return;

    _settingUpAudio = true;
    _setupRetryTimer?.cancel();

    try {
      _report('Connected - setting up audio...');

      await _audioSub?.cancel();
      _audioSub = null;
      _audioChar = null;

      await _batterySub?.cancel();
      _batterySub = null;
      _batteryChar = null;
      _settingsDimRatioChar = null;

      if (Platform.isAndroid && device.mtuNow < 512) {
        try {
          await device.requestMtu(512);
        } catch (_) {
          // A smaller negotiated MTU can still work; service discovery and
          // audio setup below will determine whether the link is usable.
        }
      }

      if (!device.isConnected) return;

      // BLE services and characteristics must be rediscovered after every
      // reconnect. Reusing the old characteristic object is not reliable.
      final services = await device.discoverServices();
      if (!device.isConnected) return;

      for (final service in services) {
        if (service.uuid.str128.toLowerCase() != batteryServiceUuid) continue;

        for (final characteristic in service.characteristics) {
          if (characteristic.uuid.str128.toLowerCase() ==
              batteryLevelCharacteristicUuid) {
            _batteryChar = characteristic;
            break;
          }
        }

        if (_batteryChar != null) break;
      }

      for (final service in services) {
        if (service.uuid.str128.toLowerCase() != settingsServiceUuid) continue;

        for (final characteristic in service.characteristics) {
          if (characteristic.uuid.str128.toLowerCase() ==
              settingsDimRatioCharacteristicUuid) {
            _settingsDimRatioChar = characteristic;
            break;
          }
        }

        if (_settingsDimRatioChar != null) break;
      }

      final batteryChar = _batteryChar;
      if (batteryChar != null) {
        try {
          final value = await batteryChar.read();
          if (value.isNotEmpty) {
            FlutterForegroundTask.sendDataToMain({
              'batteryLevel': value[0].clamp(0, 100),
            });
          }

          if (batteryChar.properties.notify) {
            await batteryChar.setNotifyValue(true);
            _batterySub = batteryChar.lastValueStream.listen((value) {
              if (value.isEmpty) return;

              FlutterForegroundTask.sendDataToMain({
                'batteryLevel': value[0].clamp(0, 100),
              });
            });
          }
        } catch (_) {
          // Battery reporting is optional and must never interrupt audio capture.
        }
      }

      final settingsDimRatioChar = _settingsDimRatioChar;
      if (settingsDimRatioChar != null) {
        try {
          final value = await settingsDimRatioChar.read();
          if (value.isNotEmpty) {
            FlutterForegroundTask.sendDataToMain({
              'ledBrightness': value[0].clamp(0, 100),
            });
          }
        } catch (_) {
          // LED brightness reporting is optional and must never interrupt audio capture.
        }
      }

      final omiService = services.firstWhere(
        (s) => s.uuid.str128.toLowerCase() == omiServiceUuid,
        orElse: () => throw Exception('Omi service not found on this device'),
      );

      final codecChar = omiService.characteristics.firstWhere(
        (c) => c.uuid.str128.toLowerCase() == audioCodecCharacteristicUuid,
      );
      final codecValue = await codecChar.read();
      final codec = codecValue.isNotEmpty
          ? parseOmiCodec(codecValue[0])
          : OmiAudioCodec.unknown;

      if (codec != OmiAudioCodec.opus) {
        _report('Unexpected codec: $codec');
        return;
      }

      _audioChar = omiService.characteristics.firstWhere(
        (c) =>
            c.uuid.str128.toLowerCase() == audioDataStreamCharacteristicUuid,
      );

      _writer ??= SegmentingAudioWriter(
        outputDir: outputDir,
        onStatus: _report,
        onSegmentSaved: (file, frameCount) {
          FlutterForegroundTask.sendDataToMain({
            'savedSegment': file.path.split('/').last,
            'frameCount': frameCount,
          });
        },
      );

      await _audioChar!.setNotifyValue(true);
      if (!device.isConnected) return;

      _audioSub = _audioChar!.lastValueStream.listen((value) {
        if (value.isEmpty) return;
        _writer?.addPacket(value);
      });

      _report('Listening...');
    } catch (e) {
      if (!_destroying) {
        _report('Connection setup failed: $e - retrying...');
        _scheduleSetupRetry(device);
      }
    } finally {
      _settingUpAudio = false;
    }
  }

  void _scheduleSetupRetry(BluetoothDevice device) {
    _setupRetryTimer?.cancel();
    _setupRetryTimer = Timer(const Duration(seconds: 3), () {
      if (!_destroying && device.isConnected) {
        unawaited(_configureConnectedDevice(device));
      }
    });
  }

  Future<void> _handleDisconnected() async {
    if (_destroying || _handlingDisconnect) return;

    _handlingDisconnect = true;
    _setupRetryTimer?.cancel();

    try {
      await _audioSub?.cancel();
      _audioSub = null;
      _audioChar = null;

      await _batterySub?.cancel();
      _batterySub = null;
      _batteryChar = null;
      _settingsDimRatioChar = null;

      // Preserve any conversation captured up to the moment the BLE link
      // dropped. autoConnect remains enabled on the BluetoothDevice and the
      // connected event will rebuild services/notifications when it returns.
      await _writer?.flushCurrentSegment();

      if (!_destroying) {
        _report('Disconnected - reconnecting automatically...');
      }
    } finally {
      _handlingDisconnect = false;
    }
  }

  @override
  void onRepeatEvent(DateTime timestamp) {
    // Not used - BLE connection state and retry timers are event-driven.
  }

  @override
  Future<void> onDestroy(DateTime timestamp, bool isTimeout) async {
    _destroying = true;
    _scanRetryTimer?.cancel();
    _setupRetryTimer?.cancel();

    await _connectionSub?.cancel();
    await _audioSub?.cancel();
    await _batterySub?.cancel();
    await _writer?.flushCurrentSegment();
    await _writer?.dispose();

    // Calling disconnect here intentionally disables flutter_blue_plus's
    // autoConnect only when the user actually stops the capture service.
    await _device?.disconnect();
  }

  Future<void> _setLedBrightness(int value) async {
    final characteristic = _settingsDimRatioChar;
    if (characteristic == null) return;

    final brightness = value.clamp(0, 100).toInt();

    try {
      await characteristic.write([brightness], withoutResponse: false);

      final readBack = await characteristic.read();
      if (readBack.isNotEmpty) {
        FlutterForegroundTask.sendDataToMain({
          'ledBrightness': readBack[0].clamp(0, 100),
        });
      }
    } catch (_) {
      // LED control is optional and must never interrupt audio capture.
    }
  }

  @override
  void onReceiveData(Object data) {
    if (data is! Map) return;

    if (data['action'] == 'startForceRecord') {
      final minutes = data['minutes'] as int?;
      if (minutes == null || minutes <= 0) return;
      _writer?.startForceRecording(Duration(minutes: minutes));
    }

    if (data['action'] == 'setLedBrightness') {
        final value = data['value'] as int?;
        if (value == null) return;
        unawaited(_setLedBrightness(value));
    }
  }

  @override
  void onNotificationButtonPressed(String id) {}

  @override
  void onNotificationPressed() {}

  @override
  void onNotificationDismissed() {}
}
