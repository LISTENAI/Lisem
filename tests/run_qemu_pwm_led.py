#!/usr/bin/env python3
"""Observe board LED duty without sampling or advancing PWM edges."""
import json,time
from qemu_test import Machine,ROOT
OUT=ROOT/'artifacts/qemu'/('pwm-led-'+time.strftime('%Y%m%d-%H%M%S'))

def main():
 for hart in (0,1):
  plan={'version':1,'chip':'ls2684','buttons':[],'indicators':[],
   'pwm_leds':[{'id':'lamp','pad':1,'active_level':1},{'id':'inverse','pad':21,'active_level':0}]}
  m=Machine(OUT/str(hart),board='arcs-board',assembly=plan,hart=hart,desktop=True)
  def state():return json.loads(m.qmp_command('qom-get',{'path':'/machine','property':'x-lisa-snapshot'}))['devices']
  try:
   assert not state()['lamp']['driven']
   m.write(0x47500004,1);m.write(0x46700028,2);m.write(0x46700030,2)
   assert state()['lamp']['effective_duty']==1
   m.write(0x4670002c,2);assert state()['lamp']['effective_duty']==0
   for pin in (1,21):m.write(0x47500000+pin*4,12)
   m.write(0x47300048,(59<<16)|39);m.write(0x47300028,3|(1<<19))
   before=m.command('clock_step 1');s=state()
   assert s['lamp']['effective_duty']==.6 and s['inverse']['effective_duty']==.4
   assert s['lamp']['enabled'] and s['inverse']['enabled']
   assert m.command('clock_step 1')==before+1
   m.write(0x47300048,(19<<16)|79)
   assert state()['lamp']['effective_duty']==.2
   m.write(0x47300028,64|(1<<18));assert state()['lamp']['effective_duty']==1
   m.write(0x47300028,1<<18);assert state()['lamp']['effective_duty']==0
   m.qmp_command('system_reset');assert not state()['lamp']['driven']
  finally:m.close()
 print('PWM LED: both harts, GPIO/PWM routing, polarity, stopped level, reset and observation time PASS')

if __name__=='__main__':main()
