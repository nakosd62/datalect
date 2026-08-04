#!/bin/bash

echo "Connect the phone."
sleep 5

cd ./mobile
flutter run > mobile.log 2>&1 &