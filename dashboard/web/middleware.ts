import { NextRequest, NextResponse } from 'next/server';

// Apple Watch live heart rate over Wi-Fi. The Health Data Server (HDS) watch app sends each
// reading as `PUT /` with {"data": "heartRate:72"} to whatever address it is given, so the
// address in the watch app is simply this dashboard's (192.168.1.163:3000). A PUT to the root is
// nothing else here (the page itself is a GET), so it is handed to the API's /hds route.
export function middleware(req: NextRequest) {
  if (req.method === 'PUT') {
    const url = req.nextUrl.clone();
    url.pathname = '/api/hds';
    return NextResponse.rewrite(url);
  }
  return NextResponse.next();
}

export const config = { matcher: '/' };
