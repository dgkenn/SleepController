// Minimal Web Bluetooth ambient types -- just the surface VerityBluetoothCard.tsx actually
// calls. TypeScript's DOM lib doesn't ship these (the spec is still a Working Draft), and the
// full @types/web-bluetooth package is more than this one component needs.

interface BluetoothRemoteGATTCharacteristic extends EventTarget {
  readonly value?: DataView;
  startNotifications(): Promise<BluetoothRemoteGATTCharacteristic>;
  stopNotifications(): Promise<BluetoothRemoteGATTCharacteristic>;
}

interface BluetoothRemoteGATTService {
  getCharacteristic(characteristic: string): Promise<BluetoothRemoteGATTCharacteristic>;
}

interface BluetoothRemoteGATTServer {
  readonly connected: boolean;
  connect(): Promise<BluetoothRemoteGATTServer>;
  disconnect(): void;
  getPrimaryService(service: string): Promise<BluetoothRemoteGATTService>;
}

interface BluetoothDevice extends EventTarget {
  readonly name?: string;
  readonly gatt?: BluetoothRemoteGATTServer;
}

interface BluetoothLEScanFilter {
  namePrefix?: string;
  services?: string[];
}

interface RequestDeviceOptions {
  filters?: BluetoothLEScanFilter[];
  optionalServices?: string[];
  acceptAllDevices?: boolean;
}

interface Bluetooth {
  requestDevice(options?: RequestDeviceOptions): Promise<BluetoothDevice>;
}

interface Navigator {
  readonly bluetooth: Bluetooth;
}
