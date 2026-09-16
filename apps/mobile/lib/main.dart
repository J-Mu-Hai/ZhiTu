import 'package:flutter/material.dart';

void main() {
  runApp(const ZhiTuApp());
}

class ZhiTuApp extends StatelessWidget {
  const ZhiTuApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: '知途',
      theme: ThemeData(colorSchemeSeed: Colors.teal),
      home: const Scaffold(
        body: Center(
          child: Text('骨架已就绪。下一步:实现「今天」页。'),
        ),
      ),
    );
  }
}
