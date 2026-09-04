import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_foreground_task/flutter_foreground_task.dart';
import 'package:opus_flutter/opus_flutter.dart' as opus_flutter;
import 'package:opus_dart/opus_dart.dart';
import 'package:permission_handler/permission_handler.dart';

import 'omi_task_handler.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  initOpus(await opus_flutter.load());
  FlutterForegroundTask.initCommunicationPort();
  runApp(const OmiCaptureApp());
}

class OmiCaptureApp extends StatelessWidget {
  const OmiCaptureApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Omi Capture',
      theme: ThemeData(useMaterial3: true, colorSchemeSeed: Colors.deepPurple),
      home: const CapturePage(),
    );
  }
}

class CapturePage extends StatefulWidget {
  const CapturePage({super.key});

  @override
  State<CapturePage> createState() => _CapturePageState();
}

class _CapturePageState extends State<CapturePage> {
  String _status = 'Not started';
  bool _isRunning = false;
  int? _batteryLevel;
  int? _ledBrightness;
  final List<String> _savedSegments = [];

  @override
  void initState() {
    super.initState();
    FlutterForegroundTask.addTaskDataCallback(_onReceiveTaskData);
    WidgetsBinding.instance.addPostFrameCallback((_) {
      _requestPermissions();
      _initService();
    });
  }

  @override
  void dispose() {
    FlutterForegroundTask.removeTaskDataCallback(_onReceiveTaskData);
    super.dispose();
  }

  void _onReceiveTaskData(Object data) {
    if (data is! Map<String, dynamic>) return;
    if (data['status'] != null) {
      setState(() => _status = data['status']);
    }
    if (data['batteryLevel'] != null) {
      setState(() => _batteryLevel = data['batteryLevel'] as int);
    }
    if (data['ledBrightness'] != null) {
      setState(() => _ledBrightness = data['ledBrightness'] as int);
    }
    if (data['savedSegment'] != null) {
      setState(() => _savedSegments.insert(0, '${data['savedSegment']} (${data['frameCount']} frames)'));
    }
  }

  Future<void> _requestPermissions() async {
    // Android 13+ requires explicit permission just to show the
    // foreground service's own persistent notification.
    final notificationPermission = await FlutterForegroundTask.checkNotificationPermission();
    if (notificationPermission != NotificationPermission.granted) {
      await FlutterForegroundTask.requestNotificationPermission();
    }

    await [
      Permission.bluetoothScan,
      Permission.bluetoothConnect,
      Permission.manageExternalStorage,
    ].request();

    if (Platform.isAndroid) {
      // Without this, Android's battery optimization can still kill the
      // service over time even with the foreground notification showing -
      // this is a real, separate protection layer, not redundant with it.
      if (!await FlutterForegroundTask.isIgnoringBatteryOptimizations) {
        await FlutterForegroundTask.requestIgnoreBatteryOptimization();
      }
    }
  }

  void _initService() {
    FlutterForegroundTask.init(
      androidNotificationOptions: AndroidNotificationOptions(
        channelId: 'omi_capture_service',
        channelName: 'Omi Capture',
        channelDescription: 'Shows while Omi Capture is listening in the background.',
        onlyAlertOnce: true,
      ),
      iosNotificationOptions: const IOSNotificationOptions(),
      foregroundTaskOptions: ForegroundTaskOptions(
        eventAction: ForegroundTaskEventAction.nothing(),
        autoRunOnBoot: false,
        allowWakeLock: true,
        allowWifiLock: true,
      ),
    );
  }

  Future<void> _startService() async {
    if (await FlutterForegroundTask.isRunningService) {
      await FlutterForegroundTask.restartService();
    } else {
      await FlutterForegroundTask.startService(
        serviceId: 256,
        notificationTitle: 'Omi Capture',
        notificationText: 'Starting...',
        callback: startCallback,
      );
    }
    setState(() => _isRunning = true);
  }

  Future<void> _stopService() async {
    await FlutterForegroundTask.stopService();
    setState(() {
      _isRunning = false;
      _status = 'Stopped';
    });
  }

  void _showForceRecordDialog() {
    showDialog(
      context: context,
      builder: (context) {
        return AlertDialog(
          title: const Text('Force Record'),
          content: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              const Text('Records for a fixed time regardless of silence - for situations like a presentation you\'re attending but not speaking in.'),
              const SizedBox(height: 16),
              Wrap(
                spacing: 8,
                runSpacing: 8,
                children: [10, 15, 30, 45, 60].map((minutes) {
                  return ElevatedButton(
                    onPressed: () {
                      Navigator.of(context).pop();
                      _startForceRecord(minutes);
                    },
                    child: Text('$minutes min'),
                  );
                }).toList(),
              ),
              const SizedBox(height: 8),
              TextButton(
                onPressed: () {
                  Navigator.of(context).pop();
                  _showCustomDurationDialog();
                },
                child: const Text('Custom...'),
              ),
            ],
          ),
          actions: [
            TextButton(onPressed: () => Navigator.of(context).pop(), child: const Text('Cancel')),
          ],
        );
      },
    );
  }

  void _showCustomDurationDialog() {
    final controller = TextEditingController();
    showDialog(
      context: context,
      builder: (context) {
        return AlertDialog(
          title: const Text('Custom duration'),
          content: TextField(
            controller: controller,
            keyboardType: TextInputType.number,
            decoration: const InputDecoration(labelText: 'Minutes'),
            autofocus: true,
          ),
          actions: [
            TextButton(onPressed: () => Navigator.of(context).pop(), child: const Text('Cancel')),
            TextButton(
              onPressed: () {
                final minutes = int.tryParse(controller.text);
                Navigator.of(context).pop();
                if (minutes != null && minutes > 0) {
                  _startForceRecord(minutes);
                }
              },
              child: const Text('Start'),
            ),
          ],
        );
      },
    );
  }

  void _startForceRecord(int minutes) {
    FlutterForegroundTask.sendDataToTask({'action': 'startForceRecord', 'minutes': minutes});
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('Omi Capture')),
      body: Padding(
        padding: const EdgeInsets.all(24.0),
        child: Column(
          children: [
            Text(
              _status,
              textAlign: TextAlign.center,
              style: Theme.of(context).textTheme.bodyLarge,
            ),
            if (_batteryLevel != null) ...[
              const SizedBox(height: 8),
              Text(
                'Omi battery: $_batteryLevel%',
                style: Theme.of(context).textTheme.bodyMedium,
              ),
            ],
            if (_ledBrightness != null) ...[
              const SizedBox(height: 8),
              Text(
                'LED brightness: $_ledBrightness%',
                style: Theme.of(context).textTheme.bodyMedium,
              ),
              Slider(
                value: _ledBrightness!.toDouble(),
                min: 0,
                max: 100,
                divisions: 100,
                label: '$_ledBrightness%',
                onChanged: (value) {
                  setState(() => _ledBrightness = value.round());
                },
                onChangeEnd: (value) {
                  FlutterForegroundTask.sendDataToTask({
                    'action': 'setLedBrightness',
                    'value': value.round(),
                  });
                },
              ),
            ],
            const SizedBox(height: 24),
            if (!_isRunning)
              ElevatedButton(onPressed: _startService, child: const Text('Start Listening'))
            else ...[
              ElevatedButton(
                onPressed: _stopService,
                style: ElevatedButton.styleFrom(backgroundColor: Colors.red.shade100),
                child: const Text('Stop Listening'),
              ),
              const SizedBox(height: 12),
              OutlinedButton(
                onPressed: _showForceRecordDialog,
                child: const Text('Force Record...'),
              ),
            ],
            const SizedBox(height: 24),
            if (_savedSegments.isNotEmpty) ...[
              const Align(alignment: Alignment.centerLeft, child: Text('Saved conversations:', style: TextStyle(fontWeight: FontWeight.bold))),
              const SizedBox(height: 8),
              Expanded(
                child: ListView.builder(
                  itemCount: _savedSegments.length,
                  itemBuilder: (context, i) => Text(_savedSegments[i]),
                ),
              ),
            ],
          ],
        ),
      ),
    );
  }
}
