/* Queue/log behavior of the real console implementation, with no UART task. */
#include <assert.h>
#include <stdarg.h>
#include <stdio.h>
#include <string.h>
#include "../main/console.c"
static int checks,failures;
#define CHECK(c) do { checks++; if (!(c)) { failures++; printf("FAIL %d: %s\n",__LINE__,#c); } } while (0)
struct mock_mutex {int available;};
static struct mock_mutex mutex;
struct mock_queue {unsigned count,head,capacity,size;char data[COMMAND_QUEUE][sizeof(input_t)];};
static struct mock_queue queue;
SemaphoreHandle_t xSemaphoreCreateMutex(void){mutex.available=1;return &mutex;}
BaseType_t xSemaphoreTake(SemaphoreHandle_t m,TickType_t t){(void)t;assert(m->available);m->available=0;return 1;}
BaseType_t xSemaphoreGive(SemaphoreHandle_t m){assert(!m->available);m->available=1;return 1;}
QueueHandle_t xQueueCreate(UBaseType_t n,UBaseType_t size){assert(n==COMMAND_QUEUE && size==sizeof(input_t));memset(&queue,0,sizeof queue);queue.capacity=n;queue.size=size;return &queue;}
BaseType_t xQueueSend(QueueHandle_t q,const void*p,TickType_t t){(void)t;if(q->count==q->capacity)return 0;memcpy(q->data[(q->head+q->count)%q->capacity],p,q->size);q->count++;return 1;}
BaseType_t xQueueReceive(QueueHandle_t q,void*p,TickType_t t){(void)t;if(!q->count)return 0;memcpy(p,q->data[q->head],q->size);q->head=(q->head+1)%q->capacity;q->count--;return 1;}
void vTaskDelay(TickType_t t){(void)t;}
BaseType_t xTaskCreatePinnedToCore(void(*f)(void*),const char*n,uint32_t s,void*a,UBaseType_t p,void*h,BaseType_t c)
{(void)f;(void)n;(void)s;(void)a;(void)p;(void)h;(void)c;return pdPASS;}
int main(void)
{
    console_init();char out[GA_COMMAND_MAX];
    CHECK(console_input_push("@F1000000\r\n",11));
    CHECK(console_read_command(out,sizeof out,0)==9);CHECK(!strcmp(out,"@F1000000"));
    CHECK(!console_input_push("v\ng 325",7));CHECK(console_read_command(out,sizeof out,0)==0);
    CHECK(!console_input_push("g\0 325",6));
    char longline[GA_COMMAND_MAX+2];memset(longline,'g',sizeof longline);
    CHECK(!console_input_push(longline,sizeof longline));CHECK(queue.count==0);
    for(int i=0;i<COMMAND_QUEUE;i++) CHECK(console_input_push("v",1));
    CHECK(!console_input_push("z",1));CHECK(queue.count==COMMAND_QUEUE);
    for(int i=0;i<COMMAND_QUEUE;i++){CHECK(console_read_command(out,sizeof out,0)==1);CHECK(!strcmp(out,"v"));}
    CHECK(console_read_command(out,sizeof out,0)==0);
    uint32_t pos=100000;CHECK(console_log_read(&pos,out,sizeof out)==0 && pos==0);
    console_printf("hello %d\n",12);CHECK(console_log_head()==9);
    pos=0;CHECK(console_log_read(&pos,out,sizeof out)==9 && !memcmp(out,"hello 12\n",9));
    s_seq=20000;memset(s_log,'A',sizeof s_log);pos=0;
    CHECK(console_log_read(&pos,out,16)==16);CHECK(pos==20000-LOG_RING+16);
    pos=UINT32_MAX;CHECK(console_log_read(&pos,out,1)==1 && pos==20000-LOG_RING+1);
    CHECK(console_log_read(&pos,out,-1)==0);
    printf("console: %d checks, %d failures\n",checks,failures);return failures?1:0;
}
