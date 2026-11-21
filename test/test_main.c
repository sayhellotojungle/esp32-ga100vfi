/* Real command/runtime code with all external effects replaced by local models.
 * This verifies parsing, mutual exclusion, failure cleanup and status publication;
 * it does not validate the ESP32 SDK, electrical behavior or injection timing. */
#include <assert.h>
#include <stdarg.h>
#include <stdio.h>
#include <string.h>
#include "../main/main.c"
#undef printf
static int checks,failures;
#define CHECK(c) do { checks++; if (!(c)) { failures++; printf("FAIL %d: %s\n",__LINE__,#c); } } while (0)
struct mock_mutex {int available;};
static struct mock_mutex locks[32];
static int nlocks,write_calls,fail_reg=-1,fail_read_reg=-1,task_core,task_priority;
/* Task creation is recorded per task name: the executor must stay core-pinned at its
 * priority, while the PEX hold guard is deliberately unpinned and lower. A single shared
 * "last task" variable would make adding any second task look like a regression. */
static int exec_core=-1,exec_priority=-1,guard_priority=-1,guard_pinned=-1;
static bool reset_low,bus_held,network_called;
static int64_t clock_us;
static esp_reset_reason_t reset_reason=ESP_RST_POWERON;
static uint32_t requested_baud=400000,actual_baud=400000;
static char log_text[8192],last_bus_command[GA_COMMAND_MAX];
static size_t log_used;
SemaphoreHandle_t xSemaphoreCreateMutex(void){assert(nlocks<32);locks[nlocks].available=1;return &locks[nlocks++];}
BaseType_t xSemaphoreTake(SemaphoreHandle_t m,TickType_t t){(void)t;if(!m->available)return 0;m->available=0;return 1;}
BaseType_t xSemaphoreGive(SemaphoreHandle_t m){assert(!m->available);m->available=1;return 1;}
void vTaskDelay(TickType_t t){clock_us+=(int64_t)t*1000;}
BaseType_t xTaskCreatePinnedToCore(void(*f)(void*),const char*n,uint32_t stack,void*a,UBaseType_t p,void*h,BaseType_t c)
{(void)f;(void)stack;(void)a;(void)h;task_core=c;task_priority=(int)p;
 if(n&&strcmp(n,"ga_executor")==0){exec_core=c;exec_priority=(int)p;}return pdPASS;}
BaseType_t xTaskCreate(void(*f)(void*),const char*n,uint32_t stack,void*a,UBaseType_t p,void*h)
{(void)f;(void)stack;(void)a;(void)h;task_priority=(int)p;
 if(n&&strcmp(n,"ga_pex_guard")==0){guard_priority=(int)p;guard_pinned=0;}return pdPASS;}
int64_t esp_timer_get_time(void){clock_us+=10;return clock_us;}
esp_err_t esp_timer_create(const esp_timer_create_args_t*c,esp_timer_handle_t*h){(void)c;*h=(void*)1;return ESP_OK;}
esp_err_t esp_timer_start_periodic(esp_timer_handle_t h,uint64_t n){(void)h;(void)n;return ESP_OK;}
esp_reset_reason_t esp_reset_reason(void){return reset_reason;}
void esp_restart(void){}
uint32_t esp_random(void){static uint32_t s=120;return ++s;}
esp_err_t gpio_set_direction(int p,int d){(void)p;(void)d;return ESP_OK;}
esp_err_t gpio_set_pull_mode(int p,int d){(void)p;(void)d;return ESP_OK;}
void console_init(void){}
int console_read_command(char*p,size_t n,int t){(void)p;(void)n;(void)t;return 0;}
int console_printf(const char*fmt,...){va_list a;va_start(a,fmt);int n=vsnprintf(log_text+log_used,sizeof log_text-log_used,fmt,a);va_end(a);if(n>0){size_t add=(size_t)n;if(add>=sizeof log_text-log_used)add=sizeof log_text-log_used-1;log_used+=add;}return n;}
void net_init(void){network_called=true;}
void net_wifi_cmd(const char*s){(void)s;}
void pex_init(void){reset_low=false;}
void pex_hold(bool low){reset_low=low;}
/* The guard's only input. Modelled from the pad state plus a clock the test winds: a
 * held pad that never comes back is precisely the forgotten-'j' failure. */
static int64_t pex_held_override_us;
int64_t pex_held_us(void){ return reset_low ? pex_held_override_us : 0; }
bool pmbus_up(void){return !bus_held;}
void pmbus_scl_hold(bool low){bus_held=low;}
uint32_t pmbus_baud_hz(void){return actual_baud;}
uint32_t pmbus_requested_baud_hz(void){return requested_baud;}
uint32_t pmbus_write_us(void){return glitch_pmbus_w_us(actual_baud);}
// The controller models its own write protect: while it is armed, every register
// the restore path needs nacks, and only the password clears it. Without this the
// firmware could pass a suite that never exercises the case where the VRM re-armed its
// protect between the last pulse and the hand-back.
//
// vrm_fault is the half that used to be missing, and the half that cost a day: a latched
// fault (exactly what our own over/under-voltage pulse produces) makes the part answer
// reads and *refuse the password*. Only CLEAR_FAULTS lets it in, and 0x03 is a plain
// command, not a protected write, so the write protect does not gate it. With the old
// password-first order this state is unreachable-from-itself: 20 no-op shots, and the
// bench concluded "the VRM latches solid, pull VDD33" -- which was wrong.
static int vrm_locked,vrm_fault;
/* Register file: the firmware writes config-id 0x46=0x1229 and reads it back, so a
 * fixture that answers every read with zero reports UNLOCK_CONFIGID on every shot.
 * Writes land here only when the controller accepts them, which is what makes the
 * read-back assertion mean something. */
static uint8_t regs[256];
/* devinit re-arm: the part ACKs the config-id write and keeps 0x1228 anyway. Only the
 * read-back catches this, which is why the firmware compares bytes instead of trusting ACK. */
static int cfg_devinit_locked;
static bool protected_reg(uint8_t r){return r==0x13||r==0x1F||r==0x21||r==0xE1||r==0x46;}
bool pmbus_w(uint8_t reg,const uint8_t*d,int n){
    write_calls++;clock_us+=100;
    if(bus_held||reg==fail_reg) return false;
    if(reg==0x03){vrm_fault=0;return true;}                  // plain command, never gated
    if(vrm_locked&&protected_reg(reg)) return false;
    if(reg==0xF2){if(vrm_fault)return false;vrm_locked=0;}   // the password took
    if(cfg_devinit_locked&&reg==0x46) return true;           // ACKed, and not stored
    if(n>=1)regs[reg]=d[0];
    if(n>=2)regs[(uint8_t)(reg+1)]=d[1];
    return true;}
bool pmbus_w16(uint8_t r,uint16_t v){uint8_t d[2]={(uint8_t)v,(uint8_t)(v>>8)};return pmbus_w(r,d,2);}
bool pmbus_r(uint8_t reg,uint8_t*d,int n){memset(d,0,(size_t)n);clock_us+=100;
    if(reg==0x7e)d[0]=(vrm_locked?0:8)|(vrm_fault?0x20:0);
    if(reg==0x46&&n>=2){d[0]=cfg_devinit_locked?0x28:regs[0x46];d[1]=cfg_devinit_locked?0x12:regs[0x47];}
    return !bus_held && reg!=fail_read_reg;}
bool pmbus_raw_write(uint8_t a,const uint8_t*b,int n){(void)a;(void)b;(void)n;return false;}
bool pmbus_raw_read(uint8_t a,uint8_t*b,int n){(void)a;(void)b;(void)n;return false;}
void pmbus_cmd(char*s){snprintf(last_bus_command,sizeof last_bus_command,"%s",s);}

static void command(const char*s){char b[GA_COMMAND_MAX];snprintf(b,sizeof b,"%s",s);dispatch(b);}
int main(void)
{
    ga_app_main();CHECK(exec_core==1 && exec_priority==8);
    /* The PEX hold guard must exist, be created, and sit below the executor: it exists so
     * a forgotten 'j' cannot leave the card in reset (see GA_PEX_MAX_HOLD_MS). */
    CHECK(guard_pinned==0 && guard_priority>=0 && guard_priority<exec_priority);
    CHECK(!board_is_busy());CHECK(!network_called);
    /* A forgotten 'j' must not be able to hold reset forever: on 2026-11-21 a 10 s manual
     * hold left the card unreachable and the box needed a power cycle, and nothing but a
     * person's memory distinguished that from a dead card. Drive it through the real
     * command path, then the guard, so the ceiling itself (not just the constant) is
     * what the build is testing. */
    command("j"); CHECK(reset_low);
    pex_held_override_us = (int64_t)GA_PEX_MAX_HOLD_MS * 1000 + 1;
    CHECK(pex_guard_once()); CHECK(!reset_low);
    command("j"); CHECK(reset_low);
    pex_held_override_us = 1000;                     /* plan-sized hold: must be left alone */
    CHECK(!pex_guard_once()); CHECK(reset_low);
    command("J"); CHECK(!reset_low);
    pex_held_override_us = 0;
    command("@F1000000");CHECK(!strcmp(last_bus_command,"F1000000"));
    command("@F400000");CHECK(!strcmp(last_bus_command,"F400000"));
    CHECK(board_begin_maintenance());CHECK(board_is_busy());CHECK(!board_begin_maintenance());
    int before=write_calls;command("g");CHECK(write_calls==before);
    command("@C start max=1");CHECK(!campaign_is_running());
    board_end_maintenance();CHECK(!board_is_busy());
    command("@C start delay=90,117 max=1 mode=1");CHECK(campaign_is_running());
    command("r");CHECK(!reset_low);CHECK(write_calls==before);
    campaign_stop();campaign_poll();CHECK(!campaign_is_running());
    glitch_args_t a;glitch_args_defaults(&a);a.fast=0;a.revert=0;
    a.vd=-1;CHECK(!board_execute_glitch(&a,NULL,NULL));CHECK(write_calls==before);
    a.vd=325;
    int failed=-1;uint32_t seq=0;
    CHECK(board_execute_glitch(&a,&failed,&seq));CHECK(failed==0 && seq!=0 && s_last.valid);
    // The config-id is part of unlocking, not a follow-up nicety: without 0x46=0x1229 the
    // GPU's devinit re-locks the VRM after the first pulse releases PEX_RST, and every later
    // shot dies at the unlock gate. Assert the part actually holds the value (bus ACK alone
    // never proves a write landed), and that the firmware checks the read-back.
    CHECK(regs[0x46]==0x29&&regs[0x47]==0x12);
    CHECK(!reset_low && !board_is_busy());
    fail_reg=0x21;CHECK(board_execute_glitch(&a,&failed,&seq));CHECK(failed>0);
    CHECK(!reset_low && !bus_held && !board_is_busy());fail_reg=-1;
    fail_read_reg=0x78;CHECK(!board_execute_glitch(&a,&failed,&seq));CHECK(!s_last.diag_ok);fail_read_reg=-1;
    char js[768];board_status_json(js,sizeof js);CHECK(strstr(js,"\"diag_ok\":false")!=NULL);
    CHECK(strstr(js,"\"reset_timing\":\"software\"")!=NULL);
    struct {char out[4];char guard;} tiny={{0},'X'};board_status_json(tiny.out,sizeof tiny.out);CHECK(tiny.guard=='X');
    // A fault latch is the state our own pulse leaves behind, and it is not the same thing
    // as a dead part: it answers reads and refuses the password *only while the fault
    // stands*. GA_CLR lives in the timeline and the timeline is gated behind the password,
    // so asking the password first can never get out -- which is exactly how twenty
    // attempts went out as no-ops while PEX_RST never moved. CLEAR_FAULTS before the
    // password is the whole fix, and this is the test that keeps it fixed.
    vrm_locked=1;vrm_fault=1;fail_reg=-1;failed=-1;
    CHECK(board_execute_glitch(&a,&failed,&seq));CHECK(failed==0 && !reset_low);
    CHECK(!vrm_locked && !vrm_fault);
    vrm_locked=1;vrm_fault=0;failed=0;
    CHECK(board_execute_glitch(&a,&failed,&seq));CHECK(failed==0);   // plain protect, no fault
    // A part that really does refuse the password (not a fault) must not silently look like
    // a fired shot: no reset pulse, and a reported failure the host can act on.
    vrm_locked=1;fail_reg=0xF2;failed=0;
    CHECK(!board_execute_glitch(&a,&failed,&seq));CHECK(!reset_low);
    fail_reg=-1;vrm_locked=0;failed=0;
    CHECK(board_execute_glitch(&a,&failed,&seq));CHECK(failed==0);   // and it recovers on its own
    // config-id refused outright (write NACK) and config-id ACKed-but-not-stored (devinit
    // re-arm) are the two ways the unlock can be incomplete. Both must stop the shot before
    // PEX_RST moves: firing without 0x1229 is what produced a round of twelve where only the
    // first attempt was real, and the verdict cannot tell that apart.
    vrm_locked=1;fail_reg=0x46;failed=0;
    CHECK(!board_execute_glitch(&a,&failed,&seq));CHECK(!reset_low);
    fail_reg=-1;vrm_locked=0;failed=0;
    cfg_devinit_locked=1;
    CHECK(!board_execute_glitch(&a,&failed,&seq));CHECK(!reset_low);
    cfg_devinit_locked=0;failed=0;
    CHECK(board_execute_glitch(&a,&failed,&seq));CHECK(failed==0);   // recover
    CHECK(regs[0x46]==0x29&&regs[0x47]==0x12);
    s_fx.cookie=FX_COOKIE;s_fx.cookie_inv=~FX_COOKIE;s_fx.phase=PH_GLITCH_RUN;
    s_fx.magic=FX_MAGIC_WDT;s_fx.boot_count=7;reset_reason=ESP_RST_SW;
    reset_history_init();CHECK(strstr(reset_note,"WATCHDOG") && strstr(reset_note,"phase=3"));
    CHECK(s_fx.phase==PH_IDLE && s_fx.boot_count==8);
    printf("runtime: %d checks, %d failures\n",checks,failures);return failures?1:0;
}
