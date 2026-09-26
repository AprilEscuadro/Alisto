// FOR THE APP REPO (Alisto_App), not the Pi -- hand this to whoever
// maintains lib/screens/device_registration_screens.dart.
//
// New screen: guides the customer to connect their phone to the Alisto
// device's temporary setup hotspot, then collects their real home WiFi
// name/password and sends it to the device.
//
// SETUP NEEDED IN THIS REPO:
//   1. Add to pubspec.yaml under dependencies:  http: ^1.2.0
//      then run: flutter pub get
//   2. Import and push this screen BEFORE RegisterDeviceStep1Screen --
//      e.g. in whatever handles the "I have an Alisto device" -> Continue
//      action (the role-selection screen), push WifiSetupScreen first;
//      from WifiSetupScreen's Continue button, push
//      RegisterDeviceStep1Screen next (unchanged, still "Step 1 of 3" --
//      this WiFi step intentionally isn't counted in that indicator so
//      nothing else needs renumbering).
//
// How it talks to the device: the Pi broadcasts an OPEN hotspot named
// "ALISTO-Setup-XXXX" (unique last 4 hex digits of that unit's WiFi MAC)
// running a tiny local web server. Once the phone joins that hotspot, this
// screen POSTs {"ssid": ..., "password": ...} as JSON to
// http://<pi's hotspot gateway IP>/wifi -- nmcli's default hotspot gateway
// is 10.42.0.1, hardcoded below since it's only reachable while the phone
// is actually on that specific hotspot.

import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;

import '../theme/app_theme.dart';
import '../widgets/common_widgets.dart';

const String _deviceHotspotGatewayIp = '10.42.0.1';
const int _devicePort = 5000;

class WifiSetupScreen extends StatefulWidget {
  final Widget nextScreen; // where to go once WiFi is confirmed working
  const WifiSetupScreen({super.key, required this.nextScreen});

  @override
  State<WifiSetupScreen> createState() => _WifiSetupScreenState();
}

class _WifiSetupScreenState extends State<WifiSetupScreen> {
  final _ssidController = TextEditingController();
  final _passwordController = TextEditingController();
  bool _connectedToHotspot = false; // step 1 -> step 2 within this screen
  bool _loading = false;
  String? _error;

  @override
  void dispose() {
    _ssidController.dispose();
    _passwordController.dispose();
    super.dispose();
  }

  Future<void> _submitHomeWifi() async {
    if (_ssidController.text.trim().isEmpty) {
      setState(() => _error = 'Enter your home WiFi name.');
      return;
    }
    setState(() {
      _loading = true;
      _error = null;
    });

    try {
      final response = await http
          .post(
            Uri.parse('http://$_deviceHotspotGatewayIp:$_devicePort/wifi'),
            headers: {'Content-Type': 'application/json'},
            body: jsonEncode({
              'ssid': _ssidController.text.trim(),
              'password': _passwordController.text,
            }),
          )
          .timeout(const Duration(seconds: 30));

      final body = jsonDecode(response.body) as Map<String, dynamic>;
      if (response.statusCode == 200 && body['ok'] == true) {
        if (!mounted) return;
        Navigator.pushReplacement(
          context,
          MaterialPageRoute(builder: (context) => widget.nextScreen),
        );
        return;
      }
      setState(() {
        _error = body['error']?.toString() ??
            'Could not connect. Check the password and try again.';
      });
    } catch (e) {
      setState(() {
        _error =
            'Could not reach the device. Make sure your phone is still '
            'connected to the ALISTO-Setup WiFi network, then try again.';
      });
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: const MinimalBackAppBar(),
      body: SafeArea(
        top: false,
        child: ResponsiveContent(
          child: SingleChildScrollView(
            padding: const EdgeInsets.symmetric(horizontal: 24),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                const SizedBox(height: 8),
                Text(
                  'Connect Your Device to WiFi',
                  style: Theme.of(context).textTheme.headlineSmall,
                ),
                const SizedBox(height: 12),
                if (!_connectedToHotspot) ...[
                  Text(
                    'Step 1: On your phone, open WiFi settings and connect '
                    'to the network starting with "ALISTO-Setup-". It has '
                    'no password.',
                    style: Theme.of(context).textTheme.bodyMedium,
                  ),
                  const SizedBox(height: 24),
                  PrimaryButton(
                    label: "I'm Connected",
                    onPressed: () => setState(() => _connectedToHotspot = true),
                  ),
                ] else ...[
                  Text(
                    'Step 2: Enter your home WiFi details. Your device '
                    'will use this to get online.',
                    style: Theme.of(context).textTheme.bodyMedium,
                  ),
                  const SizedBox(height: 20),
                  AppTextField(
                    label: 'Home WiFi Name (SSID)',
                    hint: 'MyHomeWiFi',
                    controller: _ssidController,
                  ),
                  const SizedBox(height: 14),
                  AppTextField(
                    label: 'WiFi Password',
                    hint: '••••••••',
                    obscureText: true,
                    controller: _passwordController,
                  ),
                  if (_error != null) ...[
                    const SizedBox(height: 12),
                    Text(
                      _error!,
                      style: const TextStyle(color: AppColors.error),
                    ),
                  ],
                  const SizedBox(height: 24),
                  PrimaryButton(
                    label: 'Connect',
                    loading: _loading,
                    onPressed: _submitHomeWifi,
                  ),
                ],
                const SizedBox(height: 20),
              ],
            ),
          ),
        ),
      ),
    );
  }
}
