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
{(void)f;(void)n;(void)stack;(void)a;(void)h;task_core=c;task_priority=(int)p;return pdPASS;}
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
bool pmbus_up(void){return !bus_held;}
void pmbus_scl_hold(bool low){bus_held=low;}
uint32_t pmbus_baud_hz(void){return actual_baud;}
uint32_t pmbus_requested_baud_hz(void){return requested_baud;}
uint32_t pmbus_write_us(void){return glitch_pmbus_w_us(actual_baud);}
bool pmbus_w(uint8_t reg,const uint8_t*d,int n){(void)d;(void)n;write_calls++;clock_us+=100;return !bus_held && reg!=fail_reg;}
bool pmbus_w16(uint8_t r,uint16_t v){uint8_t d[2]={(uint8_t)v,(uint8_t)(v>>8)};return pmbus_w(r,d,2);}
bool pmbus_r(uint8_t reg,uint8_t*d,int n){memset(d,0,(size_t)n);clock_us+=100;if(reg==0x7e)d[0]=8;return !bus_held && reg!=fail_read_reg;}
bool pmbus_raw_write(uint8_t a,const uint8_t*b,int n){(void)a;(void)b;(void)n;return false;}
bool pmbus_raw_read(uint8_t a,uint8_t*b,int n){(void)a;(void)b;(void)n;return false;}
void pmbus_cmd(char*s){snprintf(last_bus_command,sizeof last_bus_command,"%s",s);}

static void command(const char*s){char b[GA_COMMAND_MAX];snprintf(b,sizeof b,"%s",s);dispatch(b);}
int main(void)
{
    ga_app_main();CHECK(task_core==1 && task_priority==8);CHECK(!board_is_busy());CHECK(!network_called);
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
    CHECK(!reset_low && !board_is_busy());
    fail_reg=0x21;CHECK(board_execute_glitch(&a,&failed,&seq));CHECK(failed>0);
    CHECK(!reset_low && !bus_held && !board_is_busy());fail_reg=-1;
    fail_read_reg=0x78;CHECK(!board_execute_glitch(&a,&failed,&seq));CHECK(!s_last.diag_ok);fail_read_reg=-1;
    char js[768];board_status_json(js,sizeof js);CHECK(strstr(js,"\"diag_ok\":false")!=NULL);
    CHECK(strstr(js,"\"reset_timing\":\"software\"")!=NULL);
    struct {char out[4];char guard;} tiny={{0},'X'};board_status_json(tiny.out,sizeof tiny.out);CHECK(tiny.guard=='X');
    s_fx.cookie=FX_COOKIE;s_fx.cookie_inv=~FX_COOKIE;s_fx.phase=PH_GLITCH_RUN;
    s_fx.magic=FX_MAGIC_WDT;s_fx.boot_count=7;reset_reason=ESP_RST_SW;
    reset_history_init();CHECK(strstr(reset_note,"WATCHDOG") && strstr(reset_note,"phase=3"));
    CHECK(s_fx.phase==PH_IDLE && s_fx.boot_count==8);
    printf("runtime: %d checks, %d failures\n",checks,failures);return failures?1:0;
}
