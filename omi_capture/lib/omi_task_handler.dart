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
/// source (their example does the same with a location stream). Whether
/// flutter_blue_plus itself keeps delivering data once the screen is off
/// is the genuine, untested unknown here - everything else in this file
/// is a straightforward port of already-confirmed-working logic.
@pragma('vm:entry-point')
void startCallback() {
  FlutterForegroundTask.setTaskHandler(OmiTaskHandler());
}

class OmiTaskHandler extends TaskHandler {
  BluetoothDevice? _device;
  BluetoothCharacteristic? _audioChar;
  StreamSubscription<List<int>>? _audioSub;
  SegmentingAudioWriter? _writer;

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

    _report('Scanning...');
    try {
      await FlutterBluePlus.adapterState.where((s) => s == BluetoothAdapterState.on).first;

      final resultsSub = FlutterBluePlus.scanResults.listen((results) async {
        for (final r in results) {
          if (r.advertisementData.serviceUuids.any((u) => u.str.toLowerCase() == omiServiceUuid) || r.advertisementData.advName.toLowerCase() == 'omi' || r.device.platformName.toLowerCase() == 'omi') {
            await FlutterBluePlus.stopScan();
            await _connect(r.device);
            break;
          }
        }
      });

      await FlutterBluePlus.startScan(timeout: const Duration(seconds: 15));
      await Future.delayed(const Duration(seconds: 15));
      resultsSub.cancel();

      if (_device == null) {
        _report('No Omi device found.');
      }
    } catch (e) {
      _report('Scan failed: $e');
    }
  }

  Future<void> _connect(BluetoothDevice device) async {
    _report('Connecting...');
    try {
      await device.connect(license: License.nonprofit);
      await device.connectionState.where((s) => s == BluetoothConnectionState.connected).first;

      if (Platform.isAndroid && device.mtuNow < 512) {
        await device.requestMtu(512);
      }

      final services = await device.discoverServices();
      final omiService = services.firstWhere(
        (s) => s.uuid.str128.toLowerCase() == omiServiceUuid,
        orElse: () => throw Exception('Omi service not found on this device'),
      );

      final codecChar = omiService.characteristics.firstWhere(
        (c) => c.uuid.str128.toLowerCase() == audioCodecCharacteristicUuid,
      );
      final codecValue = await codecChar.read();
      final codec = codecValue.isNotEmpty ? parseOmiCodec(codecValue[0]) : OmiAudioCodec.unknown;

      if (codec != OmiAudioCodec.opus) {
        _report('Unexpected codec: $codec');
        return;
      }

      _audioChar = omiService.characteristics.firstWhere(
        (c) => c.uuid.str128.toLowerCase() == audioDataStreamCharacteristicUuid,
      );
      _device = device;

      _writer = SegmentingAudioWriter(
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
      _audioSub = _audioChar!.lastValueStream.listen((value) {
        if (value.isEmpty) return;
        _writer!.addPacket(value);
      });

      device.connectionState.listen((state) {
        if (state == BluetoothConnectionState.disconnected) {
          _writer?.flushCurrentSegment();
          _report('Disconnected - will need manual restart.');
        }
      });

      _report('Listening...');
    } catch (e) {
      _report('Connection failed: $e');
    }
  }

  @override
  void onRepeatEvent(DateTime timestamp) {
    // Not used - status updates are event-driven (onStatus/onSegmentSaved
    // callbacks above), not on a fixed repeat interval.
  }

  @override
  Future<void> onDestroy(DateTime timestamp, bool isTimeout) async {
    await _audioSub?.cancel();
    await _writer?.flushCurrentSegment();
    await _writer?.dispose();
    await _device?.disconnect();
  }

  @override
  void onReceiveData(Object data) {
    if (data is! Map) return;
    if (data['action'] == 'startForceRecord') {
      final minutes = data['minutes'] as int?;
      if (minutes == null || minutes <= 0) return;
      _writer?.startForceRecording(Duration(minutes: minutes));
    }
  }

  @override
  void onNotificationButtonPressed(String id) {}

  @override
  void onNotificationPressed() {}

  @override
  void onNotificationDismissed() {}
}
