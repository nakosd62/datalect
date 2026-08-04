import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;
import 'package:shared_preferences/shared_preferences.dart';

void main() {
  runApp(const MyApp());
}

class MyApp extends StatelessWidget {
  const MyApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'CRBot Mobile',
      debugShowCheckedModeBanner: false,
      theme: ThemeData.dark().copyWith(
        scaffoldBackgroundColor: const Color(0xFF0B0F19),
        colorScheme: const ColorScheme.dark(
          primary: Color(0xFF38BDF8), // Sky Blue Accent
          secondary: Color(0xFF10B981), // Emerald Green
          surface: Color(0xFF131B2E),
          error: Color(0xFFEF4444),
        ),
        cardTheme: CardThemeData(
          color: const Color(0xFF131B2E),
          elevation: 2,
          shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.circular(12),
            side: const BorderSide(color: Color(0xFF1E293B), width: 1),
          ),
        ),
        appBarTheme: const AppBarTheme(
          backgroundColor: Color(0xFF131B2E),
          elevation: 0,
          shape: Border(bottom: BorderSide(color: Color(0xFF1E293B), width: 1)),
        ),
      ),
      home: const MainScreen(),
    );
  }
}

class MainScreen extends StatefulWidget {
  const MainScreen({super.key});

  @override
  State<MainScreen> createState() => _MainScreenState();
}

class _MainScreenState extends State<MainScreen> {
  // Config & Preference State
  String serverUrl = "http://10.0.2.2:3000"; // Default for Android Emulator
  String cockroachDbUrl = "";
  String activeModel = "";
  List<String> availableModels = [];
  String dbName = "Unknown";
  String dbUser = "Unknown";
  String sessionId = "";

  // Controllers
  final TextEditingController _promptController = TextEditingController();
  final TextEditingController _sqlController = TextEditingController();

  // App Runtime State
  bool isTranslating = false;
  bool isExecuting = false;
  String transTime = "—";
  String tokensTotal = "—";
  String execTime = "—";
  String execRows = "—";
  String statusMessage = "Ready";

  // Results State
  List<Map<String, dynamic>> queryResults = [];
  List<String> queryColumns = [];
  String? errorMessage;
  bool isExecutionError = false;

  // History State
  List<Map<String, dynamic>> translationHistory = [];

  @override
  void initState() {
    super.initState();
    _loadPreferences().then((_) {
      _fetchConfig();
      _fetchHistory();
    });
  }

  Future<void> _loadPreferences() async {
    final prefs = await SharedPreferences.getInstance();
    setState(() {
      serverUrl = prefs.getString("server_url") ?? "http://10.0.2.2:3000";
    });
  }

  Future<void> _saveServerUrl(String url) async {
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString("server_url", url);
    setState(() {
      serverUrl = url;
    });
  }

  // --- Network API Calls ---

  Future<void> _fetchConfig() async {
    try {
      final response = await http.get(
        Uri.parse("$serverUrl/api/config"),
        headers: {
          if (sessionId.isNotEmpty) 'X-Session-ID': sessionId,
        },
      );
      if (response.statusCode == 200) {
        final data = jsonDecode(response.body);
        setState(() {
          dbName = data['database_name'] ?? 'Unknown';
          dbUser = data['username'] ?? 'Unknown';
          cockroachDbUrl = data['active_database_url'] ?? '';
          activeModel = data['default_model'] ?? '';
          availableModels = List<String>.from(data['available_models'] ?? []);
          if (data['session_id'] != null) {
            sessionId = data['session_id'];
          }
        });
      }
    } catch (e) {
      debugPrint("Error fetching config: $e");
    }
  }

  Future<void> _saveConfigBackend(String newDbUrl, String model) async {
    try {
      final response = await http.post(
        Uri.parse("$serverUrl/api/config"),
        headers: {
          'Content-Type': 'application/json',
          if (sessionId.isNotEmpty) 'X-Session-ID': sessionId,
        },
        body: jsonEncode({
          'database_url': newDbUrl,
          'gemini_model': model,
        }),
      );
      if (response.statusCode == 200) {
        final data = jsonDecode(response.body);
        setState(() {
          dbName = data['database_name'] ?? 'Unknown';
          dbUser = data['username'] ?? 'Unknown';
          cockroachDbUrl = data['active_database_url'] ?? '';
          activeModel = data['default_model'] ?? '';
        });
        if (mounted) {
          ScaffoldMessenger.of(context).showSnackBar(
            const SnackBar(content: Text("Configuration saved successfully")),
          );
        }
      }
    } catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text("Failed to save config: $e")),
        );
      }
    }
  }

  Future<void> _fetchHistory() async {
    try {
      final response = await http.get(Uri.parse("$serverUrl/api/history"));
      if (response.statusCode == 200) {
        final data = jsonDecode(response.body);
        if (data['success'] == true) {
          setState(() {
            translationHistory = List<Map<String, dynamic>>.from(data['history'] ?? []);
          });
        }
      }
    } catch (e) {
      debugPrint("Error fetching history: $e");
    }
  }

  Future<void> _translatePrompt({bool autoExecute = false}) async {
    final prompt = _promptController.text.trim();
    if (prompt.isEmpty) return;

    setState(() {
      isTranslating = true;
      statusMessage = "Translating...";
      errorMessage = null;
      isExecutionError = false;
      _sqlController.clear();
      queryResults.clear();
      queryColumns.clear();
    });

    try {
      final response = await http.post(
        Uri.parse("$serverUrl/api/translate"),
        headers: {
          'Content-Type': 'application/json',
          if (sessionId.isNotEmpty) 'X-Session-ID': sessionId,
        },
        body: jsonEncode({
          'prompt': prompt,
          'gemini_model': activeModel,
          'database_url': '',
        }),
      );

      final data = jsonDecode(response.body);
      if (response.statusCode == 200 && data['success'] == true) {
        setState(() {
          _sqlController.text = data['sql'] ?? '';
          transTime = "${data['duration']} ms";
          tokensTotal = "${data['total_tokens']}";
          statusMessage = "Translated";
          isTranslating = false;
        });

        // Trigger history refresh
        _fetchHistory();

        if (autoExecute) {
          _executeSql();
        }
      } else {
        setState(() {
          errorMessage = data['error'] ?? "An error occurred during translation";
          statusMessage = "Error";
          isTranslating = false;
        });
      }
    } catch (e) {
      setState(() {
        errorMessage = "Connection error: $e";
        statusMessage = "Error";
        isTranslating = false;
      });
    }
  }

  Future<void> _executeSql() async {
    final sql = _sqlController.text.trim();
    if (sql.isEmpty) return;

    setState(() {
      isExecuting = true;
      errorMessage = null;
      isExecutionError = false;
      queryResults.clear();
      queryColumns.clear();
    });

    try {
      final response = await http.post(
        Uri.parse("$serverUrl/api/execute"),
        headers: {
          'Content-Type': 'application/json',
          if (sessionId.isNotEmpty) 'X-Session-ID': sessionId,
        },
        body: jsonEncode({
          'sql': sql,
          'database_url': '',
        }),
      );

      final data = jsonDecode(response.body);
      if (response.statusCode == 200 && data['success'] == true) {
        // Assume first result block
        final resultsBlock = data['results'] != null && data['results'].isNotEmpty
            ? data['results'][0]
            : null;

        setState(() {
          execTime = "${data['executionTimeMs']} ms";
          execRows = "${data['rowCount']}";
          isExecuting = false;

          if (resultsBlock != null) {
            queryColumns = List<String>.from(resultsBlock['columns'] ?? []);
            queryResults = List<Map<String, dynamic>>.from(resultsBlock['rows'] ?? []);
          }
        });
      } else {
        setState(() {
          errorMessage = data['error'] ?? "Failed to execute query";
          isExecutionError = true;
          isExecuting = false;
        });
      }
    } catch (e) {
      setState(() {
        errorMessage = "Execution connection error: $e";
        isExecutionError = true;
        isExecuting = false;
      });
    }
  }

  // --- UI Sheets & Modals ---

  void _showSettingsModal() {
    final serverController = TextEditingController(text: serverUrl);
    final dbUrlController = TextEditingController(text: cockroachDbUrl);
    String tempModel = activeModel;

    showModalBottomSheet(
      context: context,
      isScrollControlled: true,
      backgroundColor: const Color(0xFF131B2E),
      shape: const RoundedRectangleBorder(
        borderRadius: BorderRadius.vertical(top: Radius.circular(20)),
      ),
      builder: (context) {
        return StatefulBuilder(
          builder: (context, setModalState) {
            return Padding(
              padding: EdgeInsets.only(
                bottom: MediaQuery.of(context).viewInsets.bottom,
                left: 16,
                right: 16,
                top: 24,
              ),
              child: Column(
                mainAxisSize: MainAxisSize.min,
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  const Text(
                    "Settings & Configuration",
                    style: TextStyle(fontSize: 20, fontWeight: FontWeight.bold, color: Colors.white),
                  ),
                  const SizedBox(height: 20),
                  // App Server Url
                  const Text("CRBot Mobile Server URL", style: TextStyle(color: Colors.grey)),
                  const SizedBox(height: 6),
                  TextField(
                    controller: serverController,
                    decoration: InputDecoration(
                      hintText: "http://10.0.2.2:3000",
                      filled: true,
                      fillColor: const Color(0xFF1E293B),
                      border: OutlineInputBorder(borderRadius: BorderRadius.circular(8)),
                    ),
                  ),
                  const SizedBox(height: 16),
                  // Cockroach Database URL
                  const Text("CockroachDB Connection String", style: TextStyle(color: Colors.grey)),
                  const SizedBox(height: 6),
                  TextField(
                    controller: dbUrlController,
                    decoration: InputDecoration(
                      hintText: "postgresql://postgres@host:port/database",
                      filled: true,
                      fillColor: const Color(0xFF1E293B),
                      border: OutlineInputBorder(borderRadius: BorderRadius.circular(8)),
                    ),
                  ),
                  const SizedBox(height: 16),
                  // Model Selector
                  if (availableModels.isNotEmpty) ...[
                    const Text("Gemini Translation Model", style: TextStyle(color: Colors.grey)),
                    const SizedBox(height: 6),
                    DropdownButtonFormField<String>(
                      initialValue: tempModel.isNotEmpty && availableModels.contains(tempModel) ? tempModel : availableModels.first,
                      dropdownColor: const Color(0xFF131B2E),
                      decoration: InputDecoration(
                        filled: true,
                        fillColor: const Color(0xFF1E293B),
                        border: OutlineInputBorder(borderRadius: BorderRadius.circular(8)),
                      ),
                      items: availableModels.map((model) {
                        return DropdownMenuItem(value: model, child: Text(model));
                      }).toList(),
                      onChanged: (val) {
                        if (val != null) {
                          setModalState(() => tempModel = val);
                        }
                      },
                    ),
                    const SizedBox(height: 24),
                  ],
                  Row(
                    mainAxisAlignment: MainAxisAlignment.end,
                    children: [
                      TextButton(
                        onPressed: () => Navigator.pop(context),
                        child: const Text("Cancel", style: TextStyle(color: Colors.grey)),
                      ),
                      const SizedBox(width: 12),
                      ElevatedButton(
                        style: ElevatedButton.styleFrom(backgroundColor: const Color(0xFF38BDF8)),
                        onPressed: () async {
                          final newServer = serverController.text.trim();
                          if (newServer.isNotEmpty) {
                            await _saveServerUrl(newServer);
                          }
                          await _saveConfigBackend(dbUrlController.text.trim(), tempModel);
                          if (mounted) {
                            Navigator.pop(context);
                            _fetchHistory();
                          }
                        },
                        child: const Text("Save Configuration", style: TextStyle(color: Colors.black)),
                      ),
                    ],
                  ),
                  const SizedBox(height: 24),
                ],
              ),
            );
          },
        );
      },
    );
  }

  void _showHistoryModal() {
    showModalBottomSheet(
      context: context,
      isScrollControlled: true,
      backgroundColor: const Color(0xFF0B0F19),
      shape: const RoundedRectangleBorder(
        borderRadius: BorderRadius.vertical(top: Radius.circular(20)),
      ),
      builder: (context) {
        return DraggableScrollableSheet(
          initialChildSize: 0.7,
          minChildSize: 0.5,
          maxChildSize: 0.95,
          expand: false,
          builder: (context, scrollController) {
            return Column(
              children: [
                const SizedBox(height: 12),
                Container(
                  width: 40,
                  height: 4,
                  decoration: BoxDecoration(
                    color: Colors.grey.shade700,
                    borderRadius: BorderRadius.circular(2),
                  ),
                ),
                const SizedBox(height: 16),
                const Text(
                  "Recent Translation History",
                  style: TextStyle(fontSize: 18, fontWeight: FontWeight.bold),
                ),
                const SizedBox(height: 12),
                Expanded(
                  child: translationHistory.isEmpty
                      ? const Center(child: Text("No history items found", style: TextStyle(color: Colors.grey)))
                      : ListView.builder(
                          controller: scrollController,
                          itemCount: translationHistory.length,
                          itemBuilder: (context, index) {
                            final item = translationHistory[index];
                            return Card(
                              margin: const EdgeInsets.symmetric(horizontal: 16, vertical: 6),
                              child: ListTile(
                                title: Text(
                                  item['nl_prompt'] ?? 'Prompt',
                                  style: const TextStyle(fontWeight: FontWeight.bold, fontSize: 14),
                                ),
                                subtitle: Container(
                                  margin: const EdgeInsets.only(top: 6),
                                  padding: const EdgeInsets.all(8),
                                  decoration: BoxDecoration(
                                    color: const Color(0xFF0B0F19),
                                    borderRadius: BorderRadius.circular(6),
                                  ),
                                  child: Text(
                                    item['sql_command'] ?? '',
                                    style: const TextStyle(fontFamily: 'monospace', fontSize: 12, color: Color(0xFF38BDF8)),
                                  ),
                                ),
                                onTap: () {
                                  setState(() {
                                    _promptController.text = item['nl_prompt'] ?? '';
                                    _sqlController.text = item['sql_command'] ?? '';
                                  });
                                  Navigator.pop(context);
                                },
                              ),
                            );
                          },
                        ),
                ),
              ],
            );
          },
        );
      },
    );
  }

  // --- Widgets ---

  Widget _buildStatTile(String label, String value, IconData icon) {
    return Container(
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: const Color(0xFF131B2E),
        borderRadius: BorderRadius.circular(8),
        border: Border.all(color: const Color(0xFF1E293B)),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Icon(icon, size: 14, color: const Color(0xFF38BDF8)),
              const SizedBox(width: 6),
              Text(label, style: const TextStyle(fontSize: 11, color: Colors.grey)),
            ],
          ),
          const SizedBox(height: 6),
          Text(value, style: const TextStyle(fontSize: 14, fontWeight: FontWeight.bold, color: Colors.white)),
        ],
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: Row(
          children: [
            const Icon(Icons.rocket_launch, color: Color(0xFF38BDF8)),
            const SizedBox(width: 8),
            Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                const Text("CRBot Mobile", style: TextStyle(fontSize: 16, fontWeight: FontWeight.bold)),
                Text(
                  "DB: $dbName as $dbUser",
                  style: const TextStyle(fontSize: 10, color: Colors.grey),
                ),
              ],
            ),
          ],
        ),
        actions: [
          IconButton(
            icon: const Icon(Icons.history, color: Colors.white),
            onPressed: _showHistoryModal,
          ),
          IconButton(
            icon: const Icon(Icons.settings, color: Colors.white),
            onPressed: _showSettingsModal,
          ),
        ],
      ),
      body: SingleChildScrollView(
        child: Padding(
          padding: const EdgeInsets.all(16.0),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              // 1. Natural Language Prompt Section
              const Text("Ask CockroachDB", style: TextStyle(fontSize: 14, fontWeight: FontWeight.bold, color: Colors.grey)),
              const SizedBox(height: 8),
              Container(
                decoration: BoxDecoration(
                  color: const Color(0xFF131B2E),
                  borderRadius: BorderRadius.circular(10),
                  border: Border.all(color: const Color(0xFF1E293B)),
                ),
                padding: const EdgeInsets.all(12),
                child: Column(
                  children: [
                    TextField(
                      controller: _promptController,
                      maxLines: 2,
                      style: const TextStyle(fontSize: 14),
                      decoration: const InputDecoration(
                        border: InputBorder.none,
                        hintText: "e.g., Show all users created in the last 7 days...",
                      ),
                    ),
                    const Divider(color: Color(0xFF1E293B)),
                    Row(
                      mainAxisAlignment: MainAxisAlignment.spaceBetween,
                      children: [
                        Expanded(
                          child: SingleChildScrollView(
                            scrollDirection: Axis.horizontal,
                            child: Row(
                              children: [
                                const Icon(Icons.timer, size: 14, color: Colors.grey),
                                const SizedBox(width: 4),
                                Text("LLM: $transTime", style: const TextStyle(fontSize: 11, color: Colors.grey)),
                                const SizedBox(width: 10),
                                const Icon(Icons.bolt, size: 14, color: Colors.grey),
                                const SizedBox(width: 4),
                                Text("Tokens: $tokensTotal", style: const TextStyle(fontSize: 11, color: Colors.grey)),
                                const SizedBox(width: 10),
                                const Icon(Icons.info_outline, size: 14, color: Colors.grey),
                                const SizedBox(width: 4),
                                Text(
                                  statusMessage,
                                  style: TextStyle(
                                    fontSize: 11,
                                    fontWeight: FontWeight.bold,
                                    color: statusMessage == "Error"
                                        ? Colors.redAccent
                                        : (statusMessage == "Translated" ? Colors.greenAccent : Colors.grey),
                                  ),
                                ),
                              ],
                            ),
                          ),
                        ),
                        const SizedBox(width: 8),
                        Row(
                          mainAxisSize: MainAxisSize.min,
                          children: [
                            ElevatedButton(
                              style: ElevatedButton.styleFrom(
                                backgroundColor: const Color(0xFF38BDF8),
                                foregroundColor: Colors.black,
                                padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
                                shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(6)),
                              ),
                              onPressed: isTranslating ? null : () => _translatePrompt(),
                              child: isTranslating
                                  ? const SizedBox(width: 16, height: 16, child: CircularProgressIndicator(strokeWidth: 2, color: Colors.black))
                                  : const Text("Translate", style: TextStyle(fontWeight: FontWeight.bold, fontSize: 12)),
                            ),
                            const SizedBox(width: 6),
                            OutlinedButton(
                              style: OutlinedButton.styleFrom(
                                side: const BorderSide(color: Color(0xFF38BDF8)),
                                padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
                                shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(6)),
                              ),
                              onPressed: isTranslating ? null : () => _translatePrompt(autoExecute: true),
                              child: const Text("Lucky", style: TextStyle(color: Color(0xFF38BDF8), fontWeight: FontWeight.bold, fontSize: 12)),
                            ),
                          ],
                        ),
                      ],
                    ),
                  ],
                ),
              ),
              const SizedBox(height: 16),

              // 2. SQL Output Section
              const Text("Generated SQL Statement", style: TextStyle(fontSize: 14, fontWeight: FontWeight.bold, color: Colors.grey)),
              const SizedBox(height: 8),
              Container(
                decoration: BoxDecoration(
                  color: const Color(0xFF131B2E),
                  borderRadius: BorderRadius.circular(10),
                  border: Border.all(color: const Color(0xFF1E293B)),
                ),
                padding: const EdgeInsets.all(12),
                child: Column(
                  children: [
                    TextField(
                      controller: _sqlController,
                      maxLines: 3,
                      style: const TextStyle(fontFamily: 'monospace', color: Color(0xFF38BDF8), fontSize: 13),
                      decoration: const InputDecoration(
                        border: InputBorder.none,
                        hintText: "-- SQL statements will appear here. Feel free to modify.",
                      ),
                    ),
                    const Divider(color: Color(0xFF1E293B)),
                    Row(
                      mainAxisAlignment: MainAxisAlignment.spaceBetween,
                      children: [
                        Row(
                          children: [
                            const Icon(Icons.speed, size: 14, color: Colors.grey),
                            const SizedBox(width: 4),
                            Text("Exec: $execTime", style: const TextStyle(fontSize: 11, color: Colors.grey)),
                            const SizedBox(width: 12),
                            const Icon(Icons.format_list_bulleted, size: 14, color: Colors.grey),
                            const SizedBox(width: 4),
                            Text("Rows: $execRows", style: const TextStyle(fontSize: 11, color: Colors.grey)),
                          ],
                        ),
                        ElevatedButton(
                          style: ElevatedButton.styleFrom(
                            backgroundColor: const Color(0xFF10B981),
                            foregroundColor: Colors.white,
                            padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
                            shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(6)),
                          ),
                          onPressed: isExecuting ? null : _executeSql,
                          child: isExecuting
                              ? const SizedBox(width: 16, height: 16, child: CircularProgressIndicator(strokeWidth: 2, color: Colors.white))
                              : const Text("Run SQL", style: TextStyle(fontWeight: FontWeight.bold)),
                        ),
                      ],
                    ),
                  ],
                ),
              ),
              const SizedBox(height: 24),

              // 3. Error / Success Results Section
              if (errorMessage != null) ...[
                Container(
                  padding: const EdgeInsets.all(16),
                  decoration: BoxDecoration(
                    color: isExecutionError ? const Color(0x20EF4444) : const Color(0x20F59E0B),
                    borderRadius: BorderRadius.circular(10),
                    border: Border.all(color: isExecutionError ? Colors.red : Colors.orange),
                  ),
                  child: Row(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Icon(isExecutionError ? Icons.error_outline : Icons.warning_amber_rounded,
                          color: isExecutionError ? Colors.red : Colors.orange),
                      const SizedBox(width: 12),
                      Expanded(
                        child: Column(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                            Text(isExecutionError ? "SQL Execution Error" : "Translation Warning",
                                style: TextStyle(
                                    fontWeight: FontWeight.bold,
                                    color: isExecutionError ? Colors.red.shade200 : Colors.orange.shade200)),
                            const SizedBox(height: 6),
                            Text(errorMessage!, style: const TextStyle(color: Colors.white70, fontSize: 13)),
                          ],
                        ),
                      ),
                    ],
                  ),
                ),
                const SizedBox(height: 24),
              ],

              if (queryResults.isNotEmpty) ...[
                const Text("Query Results", style: TextStyle(fontSize: 14, fontWeight: FontWeight.bold, color: Colors.grey)),
                const SizedBox(height: 8),
                Container(
                  decoration: BoxDecoration(
                    color: const Color(0xFF131B2E),
                    borderRadius: BorderRadius.circular(10),
                    border: Border.all(color: const Color(0xFF1E293B)),
                  ),
                  child: SingleChildScrollView(
                    scrollDirection: Axis.horizontal,
                    child: SingleChildScrollView(
                      scrollDirection: Axis.vertical,
                      child: DataTable(
                        headingRowColor: WidgetStateProperty.all(const Color(0xFF1E293B)),
                        columns: queryColumns.map((col) {
                          return DataColumn(label: Text(col, style: const TextStyle(fontWeight: FontWeight.bold)));
                        }).toList(),
                        rows: queryResults.map((row) {
                          return DataRow(
                            cells: queryColumns.map((col) {
                              final val = row[col];
                              return DataCell(Text(
                                val != null ? val.toString() : "NULL",
                                style: TextStyle(color: val == null ? Colors.redAccent.shade100 : Colors.white),
                              ));
                            }).toList(),
                          );
                        }).toList(),
                      ),
                    ),
                  ),
                ),
                const SizedBox(height: 40),
              ],
            ],
          ),
        ),
      ),
    );
  }
}
