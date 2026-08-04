/* Mock ghidrecomp output for corpus/hello_world.cpp.
   Written by hand to look like typical Ghidra decompilation of g++ -O0 output,
   so the eval harness has an offline "raw" candidate to score. */

undefined8 main(void)

{
  bool bVar1;
  int iVar2;
  undefined8 *puVar3;
  long in_FS_OFFSET;
  undefined1 local_98 [16];
  undefined1 local_88 [32];
  undefined1 local_68 [32];
  undefined8 local_48;
  undefined8 local_40;
  undefined8 local_38;
  long local_20;

  local_20 = *(long *)(in_FS_OFFSET + 0x28);
  puVar3 = (undefined8 *)
           std::operator<<((basic_ostream *)std::cout,"Initializing system...");
  std::basic_ostream<char,std::char_traits<char>>::operator<<
            (puVar3,std::endl<char,std::char_traits<char>>);
  std::vector<User,std::allocator<User>>::vector((vector<User,std::allocator<User>> *)&local_48);
  std::allocator<char>::allocator();
  std::__cxx11::basic_string<char,std::char_traits<char>,std::allocator<char>>::basic_string
            ((basic_string<char,std::char_traits<char>,std::allocator<char>> *)local_88,"Alice",
             (allocator<char> *)local_98);
  User::User((User *)local_68,(basic_string *)local_88,0x65);
  std::vector<User,std::allocator<User>>::push_back
            ((vector<User,std::allocator<User>> *)&local_48,(User *)local_68);
  std::allocator<char>::allocator();
  std::__cxx11::basic_string<char,std::char_traits<char>,std::allocator<char>>::basic_string
            ((basic_string<char,std::char_traits<char>,std::allocator<char>> *)local_88,"Bob",
             (allocator<char> *)local_98);
  User::User((User *)local_68,(basic_string *)local_88,0x66);
  std::vector<User,std::allocator<User>>::push_back
            ((vector<User,std::allocator<User>> *)&local_48,(User *)local_68);
  local_38 = std::vector<User,std::allocator<User>>::begin
                       ((vector<User,std::allocator<User>> *)&local_48);
  local_40 = std::vector<User,std::allocator<User>>::end
                       ((vector<User,std::allocator<User>> *)&local_48);
  while( true ) {
    bVar1 = __gnu_cxx::operator!=(&local_38,&local_40);
    if (!bVar1) break;
    puVar3 = (undefined8 *)__gnu_cxx::__normal_iterator<User*,std::vector<User,std::allocator<User>>>::operator*
                       ((__normal_iterator<User*,std::vector<User,std::allocator<User>>> *)&local_38);
    User::introduce((User *)puVar3);
    iVar2 = User::calculateMagicNumber((User *)puVar3);
    puVar3 = (undefined8 *)std::operator<<((basic_ostream *)std::cout,"Magic: ");
    puVar3 = (undefined8 *)
             std::basic_ostream<char,std::char_traits<char>>::operator<<(puVar3,iVar2);
    std::basic_ostream<char,std::char_traits<char>>::operator<<
              (puVar3,std::endl<char,std::char_traits<char>>);
    __gnu_cxx::__normal_iterator<User*,std::vector<User,std::allocator<User>>>::operator++
              ((__normal_iterator<User*,std::vector<User,std::allocator<User>>> *)&local_38);
  }
  std::vector<User,std::allocator<User>>::~vector((vector<User,std::allocator<User>> *)&local_48);
  if (local_20 != *(long *)(in_FS_OFFSET + 0x28)) {
    __stack_chk_fail();
  }
  return 0;
}

void __thiscall User::introduce(User *this)

{
  basic_ostream *pbVar1;
  undefined8 uVar2;

  pbVar1 = (basic_ostream *)std::operator<<((basic_ostream *)std::cout,"Hi, I'm ");
  pbVar1 = (basic_ostream *)std::operator<<(pbVar1,(basic_string *)this);
  pbVar1 = (basic_ostream *)std::operator<<(pbVar1," (ID: ");
  pbVar1 = (basic_ostream *)
           std::basic_ostream<char,std::char_traits<char>>::operator<<
                     (pbVar1,*(int *)(this + 0x20));
  pbVar1 = (basic_ostream *)std::operator<<(pbVar1,")");
  uVar2 = std::endl<char,std::char_traits<char>>;
  std::basic_ostream<char,std::char_traits<char>>::operator<<(pbVar1,uVar2);
  return;
}

int __thiscall User::calculateMagicNumber(User *this)

{
  return *(int *)(this + 0x20) * 0x2a;
}
