using Soup;
using libremsonic;


void print_artist_array_element(Json.Array array, uint index, Json.Node element_node)
{
    var element_object = element_node.dup_object();
    stdout.printf("    %s\n", element_object.get_string_member("name"));
    return;
}
void print_index_array_element(Json.Array array, uint index, Json.Node element_node)
{
    var element_object = element_node.dup_object();
    stdout.printf("%s: \n", element_object.get_string_member("name"));
    element_object.get_array_member("artist").foreach_element(print_artist_array_element);
    return;
}


int main(string[] args)
{

    var server = new libremsonic.Server(args[1], args[2], args[3]);

    var index_array = server.get_index_array();

    index_array.foreach_element(print_index_array_element);

    return 0;
}