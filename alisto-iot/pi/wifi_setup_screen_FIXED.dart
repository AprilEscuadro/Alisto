import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;

import '../theme/app_theme.dart';
import '../widgets/common_widgets.dart';
import 'device_registration_screens.dart';

// =========================================================
// STEP 2: CONNECT DEVICE TO WIFI
// =========================================================
/// Walks the user through connecting the Alisto device (Raspberry Pi) to
/// their home WiFi, since the device has no keyboard/screen of its own.
///
/// Flow:
/// 1. User connects their PHONE to the Pi's temporary hotspot ("Alisto-Setup")
///    via their phone's normal WiFi settings.
/// 2. User comes back here and enters their HOME WiFi name + password.
/// 3. This screen sends that info to the Pi (reachable at 192.168.4.1 while
///    the Pi is in hotspot mode).
/// 4. On success, continues to PersonalInfoScreen with the serial carried
///    forward.
///
/// A "Skip for now" option is also available in case the user wants to
/// finish WiFi setup later — it goes straight to PersonalInfoScreen without
/// sending any credentials to the Pi.
class WifiSetupScreen extends StatefulWidget {
  final String serial;
  const WifiSetupScreen({super.key, required this.serial});

  @override
  State<WifiSetupScreen> createState() => _WifiSetupScreenState();
}

class _WifiSetupScreenState extends State<WifiSetupScreen> {
  final _ssidController = TextEditingController();
  final _passwordController = TextEditingController();

  bool _isConnecting = false;
  String? _statusMessage;
  bool? _lastAttemptSucceeded;

  // Default gateway IP while the Pi is broadcasting its own hotspot.
  // CLAUDE'S FIX: this was 192.168.4.1 (the typical hostapd default), but
  // `nmcli device wifi hotspot` (what the Pi's start_setup_hotspot.sh
  // actually uses) assigns 10.42.0.1 by default -- confirmed directly on
  // the real device via `ip addr show wlan0`. The mismatch meant every
  // request from the app was going to an address the phone couldn't even
  // route to on the "Alisto-Setup" network, which is why it always failed
  // with "Could not reach Alisto" no matter how many times it was retried.
  // Confirm with `ip addr show wlan0` on the Pi if this ever changes.
  static const String _piSetupUrl = 'http://10.42.0.1:5000/connect-wifi';

  @override
  void dispose() {
    _ssidController.dispose();
    _passwordController.dispose();
    super.dispose();
  }

  void _showError(String message) {
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(content: Text(message), backgroundColor: AppColors.error),
    );
  }

  Future<void> _sendWifiCredentials() async {
    final ssid = _ssidController.text.trim();
    final password = _passwordController.text;

    if (ssid.isEmpty) {
      _showError('Please enter your WiFi name.');
      return;
    }
    if (password.isEmpty) {
      _showError('Please enter your WiFi password.');
      return;
    }

    setState(() {
      _isConnecting = true;
      _statusMessage = null;
      _lastAttemptSucceeded = null;
    });

    try {
      final response = await http
          .post(
            Uri.parse(_piSetupUrl),
            headers: {'Content-Type': 'application/json'},
            body: jsonEncode({'ssid': ssid, 'password': password}),
          )
          .timeout(const Duration(seconds: 20));

      if (response.statusCode == 200) {
        final data = jsonDecode(response.body);
        final success = data['success'] == true;
        setState(() {
          _lastAttemptSucceeded = success;
          _statusMessage = success
              ? 'Success! Alisto is connecting to "$ssid". You can now '
                  'reconnect your phone to your normal WiFi and continue.'
              : (data['message'] ??
                    'Alisto could not connect. Please check the WiFi name '
                        'and password and try again.');
        });
      } else {
        setState(() {
          _lastAttemptSucceeded = false;
          _statusMessage =
              'Alisto responded with an error (code ${response.statusCode}). Please try again.';
        });
      }
    } catch (e) {
      setState(() {
        _lastAttemptSucceeded = false;
        _statusMessage =
            'Could not reach Alisto. Make sure your phone is still connected '
            'to the "Alisto-Setup" WiFi network, then try again.';
      });
    } finally {
      setState(() => _isConnecting = false);
    }
  }

  void _continueToPersonalInfo() {
    Navigator.push(
      context,
      MaterialPageRoute(
        builder: (context) => PersonalInfoScreen(serial: widget.serial),
      ),
    );
  }

  /// Skips WiFi setup entirely and continues on to personal info.
  /// The device stays in "Alisto-Setup" hotspot mode until the user
  /// configures WiFi some other way (e.g. from a settings screen later).
  void _skipWifiSetup() {
    Navigator.push(
      context,
      MaterialPageRoute(
        builder: (context) => PersonalInfoScreen(serial: widget.serial),
      ),
    );
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
                  'Connect to WiFi',
                  style: Theme.of(context).textTheme.headlineSmall,
                ),
                const SizedBox(height: 12),
                const RegisterDeviceStepIndicator(
                  currentStep: 2,
                  totalSteps: 4,
                ),
                const SizedBox(height: 24),
                _buildInstructions(context),
                const SizedBox(height: 24),
                Text(
                  'Enter your home WiFi details',
                  style: Theme.of(context).textTheme.titleMedium,
                ),
                const SizedBox(height: 14),
                AppTextField(
                  label: 'WiFi Name (SSID)',
                  hint: 'Home_WiFi_5G',
                  controller: _ssidController,
                ),
                const SizedBox(height: 14),
                AppTextField(
                  label: 'WiFi Password',
                  hint: '••••••••',
                  obscureText: true,
                  controller: _passwordController,
                ),
                const SizedBox(height: 24),
                PrimaryButton(
                  label: 'Connect Alisto to WiFi',
                  loading: _isConnecting,
                  onPressed: _sendWifiCredentials,
                ),
                const SizedBox(height: 12),
                Center(
                  child: TextButton(
                    onPressed: _isConnecting ? null : _skipWifiSetup,
                    child: const Text(
                      'Skip for now',
                      style: TextStyle(
                        color: AppColors.textSecondary,
                        fontWeight: FontWeight.w600,
                      ),
                    ),
                  ),
                ),
                if (_statusMessage != null) ...[
                  const SizedBox(height: 16),
                  Container(
                    padding: const EdgeInsets.all(12),
                    decoration: BoxDecoration(
                      color: _lastAttemptSucceeded == true
                          ? Colors.green.withOpacity(0.08)
                          : AppColors.error.withOpacity(0.08),
                      borderRadius: BorderRadius.circular(12),
                      border: Border.all(
                        color: _lastAttemptSucceeded == true
                            ? Colors.green
                            : AppColors.error,
                      ),
                    ),
                    child: Row(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Icon(
                          _lastAttemptSucceeded == true
                              ? Icons.check_circle
                              : Icons.error_outline,
                          color: _lastAttemptSucceeded == true
                              ? Colors.green
                              : AppColors.error,
                        ),
                        const SizedBox(width: 10),
                        Expanded(
                          child: Text(
                            _statusMessage!,
                            style: Theme.of(context).textTheme.bodySmall,
                          ),
                        ),
                      ],
                    ),
                  ),
                ],
                if (_lastAttemptSucceeded == true) ...[
                  const SizedBox(height: 16),
                  PrimaryButton(
                    label: 'Continue',
                    onPressed: _continueToPersonalInfo,
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

  Widget _buildInstructions(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(
          'Step 1: Connect your phone to Alisto',
          style: Theme.of(context).textTheme.titleMedium,
        ),
        const SizedBox(height: 8),
        Text(
          '1. Open your phone\'s WiFi settings.\n'
          '2. Look for a network named "Alisto-Setup" and connect to it.\n'
          '3. Come back to this screen once connected.',
          style: Theme.of(context).textTheme.bodySmall,
        ),
      ],
    );
  }
}